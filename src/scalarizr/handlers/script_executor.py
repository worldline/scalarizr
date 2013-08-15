from __future__ import with_statement
'''
Created on Dec 24, 2009

@author: marat
'''

from scalarizr.bus import bus
from scalarizr import config as szrconfig
from scalarizr.handlers import Handler, HandlerError
from scalarizr.messaging import Queues, Messages
from scalarizr.util import parse_size, format_size, read_shebang, split_strip, wait_until
from scalarizr.config import ScalarizrState
from scalarizr.handlers import operation

import time
import ConfigParser
import subprocess
import threading
import os
import shutil
import stat
import signal
import logging
import Queue


def get_handlers():
    return [ScriptExecutor()]


LOG = logging.getLogger(__name__)

skip_events = set()
"""
@var ScriptExecutor will doesn't request scripts on passed events
"""

exec_dir_prefix = '/usr/local/bin/scalr-scripting.'
logs_dir = '/var/log/scalarizr/scripting'
logs_truncate_over = 20 * 1000


class ScriptExecutor(Handler):
    name = 'script_executor'
    _data = None

    def __init__(self):
        self.queue = Queue.Queue()
        self.in_progress = []
        bus.on(
                init=self.on_init,
                start=self.on_start,
                shutdown=self.on_shutdown
        )

        # Operations
        self._op_exec_scripts = 'Execute scripts'
        self._step_exec_tpl = "Execute '%s' in %s mode"

        # Services
        self._cnf = bus.cnf
        self._queryenv = bus.queryenv_service
        self._platform = bus.platform

    def on_init(self):
        global exec_dir_prefix, logs_dir, logs_truncate_over

        bus.on(
            host_init_response=self.on_host_init_response,
            before_host_up=self.on_before_host_up
        )

        # Configuration
        cnf = bus.cnf
        ini = cnf.rawini

        # read exec_dir_prefix
        try:
            exec_dir_prefix = ini.get(self.name, 'exec_dir_prefix')
            if not os.path.isabs(exec_dir_prefix):
                os.path.join(bus.base_path, exec_dir_prefix)
        except ConfigParser.Error:
            pass

        # read logs_dir_prefix
        try:
            logs_dir = ini.get(self.name, 'logs_dir')
            if not os.path.exists(logs_dir):
                os.makedirs(logs_dir)
        except ConfigParser.Error:
            pass

        # logs_truncate_over
        try:
            logs_truncate_over = parse_size(ini.get(self.name, 'logs_truncate_over'))
        except ConfigParser.Error:
            pass

        self.log_rotate_runnable = LogRotateRunnable()
        self.log_rotate_thread = threading.Thread(name='ScriptingLogRotate',
                                                                target=self.log_rotate_runnable)
        self.log_rotate_thread.setDaemon(True)

    def on_start(self):
        # Start log rotation
        self.log_rotate_thread.start()

        # Restore in-progress scripts
        LOG.debug('STATE[script_executor.in_progress]: %s', szrconfig.STATE['script_executor.in_progress'])
        scripts = [Script(**kwds) for kwds in szrconfig.STATE['script_executor.in_progress'] or []]
        LOG.debug('Restoring %d in-progress scripts', len(scripts))

        for sc in scripts:
            self._execute_one_script(sc)

    def on_shutdown(self):
        # save state
        LOG.debug('Saving Work In Progress (%d items)', len(self.in_progress))
        szrconfig.STATE['script_executor.in_progress'] = [sc.state() for sc in self.in_progress]

    def on_host_init_response(self, hir_message):
        self._data = hir_message.body.get('base', {})
        self._data = self._data or {}
        if 'keep_scripting_logs_time' in self._data:
            self.log_rotate_runnable.keep_scripting_logs_time = int(self._data.get('keep_scripting_logs_time', 86400))

    def on_before_host_up(self, hostup):
        if not 'base' in hostup.body:
            hostup.base = {}
        hostup.base['keep_scripting_logs_time'] = self.log_rotate_runnable.keep_scripting_logs_time


    def _execute_one_script(self, script):
        if script.asynchronous:
            threading.Thread(target=self._execute_one_script0,
                                            args=(script, )).start()
        else:
            self._execute_one_script0(script)

    def _execute_one_script0(self, script):
        try:
            self.in_progress.append(script)
            if not script.start_time:
                script.start()
            self.send_message(Messages.EXEC_SCRIPT_RESULT, script.wait(), queue=Queues.LOG)
        except:
            if script.asynchronous:
                LOG.exception('Caught exception')
            raise
        finally:
            self.in_progress.remove(script)

    def execute_scripts(self, scripts):
        if not scripts:
            return


        # read logs_dir_prefix
        ini = bus.cnf.rawini
        try:
            logs_dir = ini.get(self.name, 'logs_dir')
            if not os.path.exists(logs_dir):
                os.makedirs(logs_dir)
        except ConfigParser.Error:
            pass

        if scripts[0].event_name:
            phase = "Executing %d %s script(s)" % (len(scripts), scripts[0].event_name)
        else:
            phase = 'Executing %d script(s)' % (len(scripts), )
        self._logger.info(phase)

        if self._cnf.state != szrconfig.ScalarizrState.INITIALIZING:
            # Define operation
            op = operation(name=self._op_exec_scripts, phases=[{
                    'name': phase,
                    'steps': ["Execute '%s'" % script.name for script in scripts if not script.asynchronous]
            }])
            op.define()
        else:
            op = bus.initialization_op

        with op.phase(phase):
            for script in scripts:
                step_title = self._step_exec_tpl % (script.name, 'async' if script.asynchronous else 'sync')
                with op.step(step_title):
                    self._execute_one_script(script)

    def accept(self, message, queue, behaviour=None, platform=None, os=None, dist=None):
        return not message.name in skip_events

    def __call__(self, message):
        event_name = message.event_name if message.name == Messages.EXEC_SCRIPT else message.name
        role_name = message.body.get('role_name', 'unknown_role')
        LOG.debug("Scalr notified me that '%s' fired", event_name)

        if self._cnf.state == ScalarizrState.IMPORTING:
            LOG.debug('Scripting is OFF when state: %s', ScalarizrState.IMPORTING)
            return

        scripts = []

        if 'scripts' in message.body:
            if not message.body['scripts']:
                self._logger.debug('Empty scripts list. Breaking')
                return

            LOG.debug('Fetching scripts from incoming message')
            scripts = [Script(name=item['name'], body=item['body'],
                                            asynchronous=int(item['asynchronous']),
                                            exec_timeout=item['timeout'], event_name=event_name,
                                            role_name=role_name,
                                            event_server_id=message.body.get('server_id'),
                                            event_id=message.body.get('event_id'))
                                    for item in message.body['scripts']]
        else:
            # TODO: remove obsolete code
            LOG.debug("Fetching scripts for event %s", event_name)
            event_id = message.meta['event_id'] if message.name == Messages.EXEC_SCRIPT else None
            target_ip = message.body.get('local_ip')
            local_ip = self._platform.get_private_ip()

            queryenv_scripts = self._queryenv.list_scripts(event_name, event_id,
                                                                    target_ip=target_ip, local_ip=local_ip)
            scripts = [Script(name=s.name, body=s.body, path=s.path, asynchronous=s.asynchronous,
                                    exec_timeout=s.exec_timeout, event_name=event_name, role_name=role_name) \
                                    for s in queryenv_scripts]

        global_variables = message.body.get('global_variables') or []
        for kv in global_variables:
            os.environ[kv['name']] = kv['value'] or ''

        LOG.debug('Fetched %d scripts', len(scripts))
        self.execute_scripts(scripts)


class Script(object):
    name = None
    body = None
    path = None
    asynchronous = None
    event_name = None
    role_name = None
    exec_timeout = 0
    event_server_id = None
    event_id = None

    id = None
    pid = None
    return_code = None
    interpreter = None
    start_time = None
    exec_path = None

    logger = None
    proc = None
    stdout_path = None
    stderr_path = None

    def __init__(self, **kwds):
        '''
        Variant A:
        Script(name='AppPreStart', body='#!/usr/bin/python ...', asynchronous=True)

        Variant B:
        Script(id=43432234343, name='AppPreStart', pid=12145,
                        interpreter='/usr/bin/python', start_time=4342424324, asynchronous=True)
        '''
        for key, value in kwds.items():
            setattr(self, key, value)

        assert self.name, '`name` required'
        assert self.exec_timeout, '`exec_timeout` required'

        if self.name and (self.body or self.path):
            self.id = str(time.time())
            if self.path:
                if not os.path.exists(self.path):
                    raise Exception('Script %s does not exist (path: %s)' % (self.name, self.path))
                with open(self.path) as f:
                    body = f.read()

            interpreter = read_shebang(script=self.body or body)
            if not interpreter:
                raise HandlerError("Can't execute script '%s' cause it hasn't shebang.\n"
                                                "First line of the script should have the form of a shebang "
                                                "interpreter directive is as follows:\n"
                                                "#!interpreter [optional-arg]" % (self.name, ))
            self.interpreter = interpreter
        else:
            assert self.id, '`id` required'
            assert self.pid, '`pid` required'
            assert self.start_time, '`start_time` required'
            if self.interpreter:
                self.interpreter = split_strip(self.interpreter)[0]

        self.logger = logging.getLogger('%s.%s' % (__name__, self.id))
        self.exec_path = self.path or os.path.join(exec_dir_prefix + self.id, self.name)
        if self.exec_timeout:
            self.exec_timeout = int(self.exec_timeout)
        args = (self.name, self.event_name, self.role_name, self.id)
        self.stdout_path = os.path.join(logs_dir, '%s.%s.%s.%s-out.log' % args)
        self.stderr_path = os.path.join(logs_dir, '%s.%s.%s.%s-err.log' % args)

    def start(self):
        # Check interpreter here, and not in __init__
        # cause scripts can create sequences when previous script
        # installs interpreter for the next one
        if not os.path.exists(self.interpreter):
            raise HandlerError("Can't execute script '%s' cause "
                                            "interpreter '%s' not found" % (self.name, self.interpreter))

        if not self.path:
            # Write script to disk, prepare execution
            exec_dir = os.path.dirname(self.exec_path)
            if not os.path.exists(exec_dir):
                os.makedirs(exec_dir)

            with open(self.exec_path, 'w') as fp:
                fp.write(self.body.encode('utf-8'))
            os.chmod(self.exec_path, stat.S_IREAD | stat.S_IEXEC)

        stdout = open(self.stdout_path, 'w+')
        stderr = open(self.stderr_path, 'w+')

        # Start process
        self.logger.debug('Executing %s'
                        '\n  %s'
                        '\n  1>%s'
                        '\n  2>%s'
                        '\n  timeout: %s seconds',
                        self.interpreter, self.exec_path, self.stdout_path,
                        self.stderr_path, self.exec_timeout)
        self.proc = subprocess.Popen(self.exec_path, stdout=stdout,
                                                stderr=stderr, close_fds=True)
        self.pid = self.proc.pid
        self.start_time = time.time()

    def wait(self):
        try:
            # Communicate with process
            self.logger.debug('Communicating with %s (pid: %s)', self.interpreter, self.pid)
            while time.time() - self.start_time < self.exec_timeout:
                if self._proc_poll() is None:
                    time.sleep(0.5)
                else:
                    # Process terminated
                    self.logger.debug('Process terminated')
                    self.return_code = self._proc_complete()
                    break
            else:
                # Process timeouted
                self.logger.debug('Timeouted: %s seconds. Killing process %s (pid: %s)',
                                                        self.exec_timeout, self.interpreter, self.pid)
                self.return_code = self._proc_kill()

            if not os.path.exists(self.stdout_path):
                open(self.stdout_path, 'w+').close()
            if not os.path.exists(self.stderr_path):
                open(self.stderr_path, 'w+').close()

            elapsed_time = time.time() - self.start_time
            self.logger.debug('Finished %s'
                            '\n  %s'
                            '\n  1: %s'
                            '\n  2: %s'
                            '\n  return code: %s'
                            '\n  elapsed time: %s',
                            self.interpreter, self.exec_path,
                            format_size(os.path.getsize(self.stdout_path)),
                            format_size(os.path.getsize(self.stderr_path)),
                            self.return_code,
                            elapsed_time)
            args = (self.name, self.event_name, self.role_name, self.id)
            ret = dict(
                    stdout=None,
                    stderr=None,
                    exec_script_id='%s.%s.%s.%s' % args,
                    time_elapsed=elapsed_time,
                    script_name=self.name,
                    script_path=self.exec_path,
                    event_name=self.event_name or '',
                    return_code=self.return_code,
                    event_server_id=self.event_server_id,
                    event_id=self.event_id
            )
            return ret

        except:
            if threading.currentThread().name != 'MainThread':
                self.logger.exception('Exception in script execution routine')
            else:
                raise

        finally:
            if not self.path:
                f = os.path.dirname(self.exec_path)
                if os.path.exists(f):
                    shutil.rmtree(f)

    def state(self):
        return {
                'id': self.id,
                'pid': self.pid,
                'name': self.name,
                'interpreter': self.interpreter,
                'start_time': self.start_time,
                'asynchronous': self.asynchronous,
                'event_name': self.event_name,
                'role_name': self.role_name,
                'exec_timeout': self.exec_timeout
        }

    def _proc_poll(self):
        if self.proc:
            return self.proc.poll()
        else:
            statfile = '/proc/%s/stat' % self.pid
            exefile = '/proc/%s/exe' % self.pid
            if os.path.exists(exefile) and os.readlink(exefile) == self.interpreter:
                stat = open(statfile).read().strip().split(' ')
                if stat[2] not in ('Z', 'D'):
                    return None

            return 0

    def _proc_kill(self):
        self.logger.debug('Timeouted: %s seconds. Killing process %s (pid: %s)',
                                                self.exec_timeout, self.interpreter, self.pid)
        if self.proc and self._proc_poll() is None:
            os.kill(self.pid, signal.SIGTERM)
            if not wait_until(lambda: self._proc_poll() is not None,
                            timeout=2, sleep=.5, raise_exc=False):
                os.kill(self.pid, signal.SIGKILL)
                return -9
            return self.proc.returncode

    def _proc_complete(self):
        if self.proc:
            self._proc_finalize()
            return self.proc.returncode
        else:
            return 0

    def _proc_finalize(self):
        if self.proc.stdout:
            try:
                self.proc.stdout.flush()
                os.fsync(self.proc.stdout.fileno())
            except:
                pass
        if self.proc.stderr:
            try:
                self.proc.stderr.flush()
                os.fsync(self.proc.stderr.fileno())
            except:
                pass


class LogRotateRunnable(object):
    keep_scripting_logs_time = 86400  # 1 day

    def __call__(self):
        while True:
            LOG.debug('Starting log_rotate routine')
            now = time.time()
            for name in os.listdir(logs_dir):
                filename = os.path.join(logs_dir, name)
                if os.stat(filename).st_ctime + self.keep_scripting_logs_time < now:
                    LOG.debug('Delete %s', filename)
                    os.remove(filename)
            time.sleep(3600)
