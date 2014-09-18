from __future__ import with_statement

import sys
import logging
from scalarizr import linux
from scalarizr.api import mysql
from scalarizr.linux import pkgmgr
from scalarizr.util import Singleton
from scalarizr import exceptions
from scalarizr.api import DependencyError


LOG = logging.getLogger(__name__)


class PerconaAPI(mysql.MySQLAPI):

    __metaclass__ = Singleton

    behavior = 'percona'

    _software_name = 'precona'

    def __init__(self):
        super(PerconaAPI, self).__init__()

    @classmethod
    def do_check_software(cls, system_packages=None):
        os_name = linux.os['name'].lower()
        os_vers = linux.os['version']
        if os_name == 'ubuntu' and os_vers >= '14':
            requirements = [
                ['percona-server-server-5.1', 'percona-server-client-5.1' ],
                ['percona-server-server-5.5', 'percona-server-client-5.5' ],
                ['percona-server-server-5.6', 'percona-server-client-5.6' ],
            ]
        elif linux.os.debian_family:
            requirements = [
                ['percona-server-server-5.1', 'percona-server-client-5.1'],
                ['percona-server-server-5.5', 'percona-server-client-5.5'],
            ]
        elif linux.os.redhat_family or linux.os.oracle_family:
            requirements = [
                ['Percona-Server-server-51', 'Percona-Server-client-51'],
                ['Percona-Server-server-55', 'Percona-Server-client-55'],
            ]
        else:
            raise exceptions.UnsupportedBehavior(
                    cls.behavior,
                    "apache: Not supported on {0} os family".format(linux.os['family']))
        errors = list()
        for requirement in requirements:
            try:
                installed = pkgmgr.check_software(requirement[0], system_packages)[0]
                try:
                    pkgmgr.check_software(requirement[1:], system_packages)[0]
                    return installed
                except pkgmgr.NotInstalledError:
                    e = sys.exc_info()[1]
                    raise DependencyError(e.args[0])
            except:
                e = sys.exc_info()[1]
                errors.append(e)
        for cls in [pkgmgr.VersionMismatchError, DependencyError, pkgmgr.NotInstalledError]:
            for error in errors:
                if isinstance(error, cls):
                    raise error

