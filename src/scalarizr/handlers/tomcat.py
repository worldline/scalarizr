import os


from scalarizr import handlers, linux
from scalarizr.bus import bus
from scalarizr.linux import pkgmgr
from scalarizr.messaging import Messages
from scalarizr.util import initdv2
from scalarizr.node import __node__


def get_handlers():
    return [TomcatHandler()]

class TomcatHandler(handlers.Handler, handlers.FarmSecurityMixin):

    def __init__(self):
        handlers.Handler.__init__(self)
        handlers.FarmSecurityMixin.__init__(self, [8080, 8443])
        bus.on(
            init=self.on_init, 
            start=self.on_start
        )
        self.tomcat_version = None
        self.config_dir = None
        self.init_script_path = None

        if linux.os.debian_family:
            if (linux.os['name'] == 'Ubuntu' and linux.os['version'] >= (12, 4)) or \
                (linux.os['name'] == 'Debian' and linux.os['version']  >= (7, 0)):
                self.tomcat_version = 7
            else:
                self.tomcat_version = 6
            self.config_dir = '/etc/tomcat{0}'.format(self.tomcat_version)
            self.init_script_path = '/etc/init.d/tomcat{0}'.format(self.tomcat_version)
        else:
            self.tomcat_version = 6
            self.config_dir = '/etc/tomcat'
            self.init_script_path = '/etc/init.d/tomcat'

        self.service = initdv2.ParametrizedInitScript('tomcat', self.init_script_path)

    def on_init(self):
        bus.on(
            host_init_response=self.on_host_init_response,
            before_host_up=self.on_before_host_up
        )

    def on_start(self):
        if __node__['state'] == 'running':
            self.service.start()

    def accept(self, message, queue, behaviour=None, **kwds):
        return message.name in (
                Messages.HOST_INIT, 
                Messages.HOST_DOWN) and 'tomcat' in behaviour

    def on_host_init_response(self, hir_message):
        if not os.path.exists(self.service.initd_script):
            tomcat = 'tomcat{0}'.format(self.tomcat_version)
            pkgs = [tomcat]
            if linux.os.debian_family:
                pkgs.append('{0}-admin'.format(tomcat))
            elif linux.os.redhat_family or linux.os.oracle_family:
                pkgs.append('{0}-admin-webapps'.format(tomcat))
            for pkg in pkgs:
                pkgmgr.installed(pkg)
        pkgmgr.installed('augeas-tools' if linux.os.debian_family else 'augeas')

    def on_before_host_up(self, message):
        load_lens = [
            'set /augeas/load/Xml/incl[last()+1] "{config_dir}/*.xml"',
            'load',
            'defvar service /files/{0}/server.xml/Server/Service' .format(self.config_dir)                       
        ]
        
        # Enable SSL
        augscript = '\n'.join(load_lens + [
            'print $service/Connector/*/port'
        ])
        augscript = augscript.format(config_dir=self.config_dir)
        out = linux.system(('augtool',), stdin=augscript)[1]
        if not '8443' in out:
            self.service.stop()
            augscript = '\n'.join(load_lens + [
                'defnode connector $service/Connector[last()+1]',
                'defvar attrs $connector/#attribute'
                'set $attrs/port 8443',
                'set $attrs/protocol "HTTP/1.1"',
                'set $attrs/SSLEnabled true',
                'set $attrs/maxThreads 150',
                'set $attrs/scheme https',
                'set $attrs/secure true',
                'set $attrs/clientAuth false',
                'set $attrs/sslProtocol TLS',
                'save'
            ])
            linux.system(('augtool', ), stdin=augscript)

        self.service.start()

