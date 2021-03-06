# pylint: disable=W0614
import os
import re
import time
import glob
import json
import shutil

from fabric.api import *
from fabric.decorators import runs_once
from fabric.context_managers import shell_env
from fabric.colors import green, red, yellow

env['use_ssh_config'] = True
project = os.environ.get('CI_PROJECT', 'scalarizr')
build_dir = os.environ['PWD']
home_dir = os.environ.get('CI_HOME_DIR', '/var/lib/ci')
omnibus_dir = os.path.join(build_dir, 'omnibus')
project_dir = os.path.join(home_dir, project)
rpm_deps_dir = os.path.join(project_dir, 'rpm-deps')
verbose = os.environ.get('CI_VERBOSE', 'no').lower() in ('1', 'yes', 'y')
repo_dir = '/var/www'
aptly_conf = None
aptly_prefix = None
gpg_key = '04B54A2A'
remote_repo_host = 'sl6.scalr.net'
remote_repo_user = 'root'
remote_repo_port = 60022
remote_repo_dir = '/var/www/repo'
build_number_file = os.path.join(project_dir, '.build_number')
omnibus_md5sum_file = os.path.join(project_dir, '.omnibus.md5')
permitted_artifacts_number = 2
build_number = None
artifacts_dir = None
tag = None
branch = None
version = None
repo = None


def read_build_number():
    print_green('Setting up artifacts dir')
    with open(build_number_file) as fp:
        return int(fp.read())


def setup_artifacts_dir():
    global artifacts_dir
    # append build_number to artifacts dir
    artifacts_dir = os.path.join(project_dir, str(build_number))
    local('mkdir -p {0}'.format(artifacts_dir))


@task
def prepare():
    '''
    setup next build ('prepare' script in StriderCD)
    '''
    global artifacts_dir, build_number

    # setup project dir
    if not os.path.exists(project_dir):
        os.makedirs(project_dir)
    # bump build number
    if os.path.exists(build_number_file):
        build_number = read_build_number()
    else:
        build_number = 0
    build_number += 1
    with open(build_number_file, 'w+') as fp:
        fp.write(str(build_number))
    setup_artifacts_dir()
    # setp artifacts dir
    if not os.path.exists(artifacts_dir):
        os.makedirs(artifacts_dir)
    cleanup_artifacts()
    if not os.path.exists(rpm_deps_dir):
        os.makedirs(rpm_deps_dir)
    print_green('build_number: {0}'.format(build_number))
    print_green('artifacts_dir: {0}'.format(artifacts_dir))


@runs_once
def init():
    '''
    Initialize current build.
    '''
    global tag, branch, version, repo, build_number, artifacts_dir, \
            aptly_conf, aptly_prefix

    build_number = read_build_number()
    print_green('build_number: {0}'.format(build_number))
    setup_artifacts_dir()

    if os.path.exists('.git/FETCH_HEAD'):
        with open('.git/FETCH_HEAD') as fp:
            m = re.search(r"^([0-9a-f]{8,40})\s+tag '([^']+)'", fp.read())
            revision = m.group(1)
            ref = m.group(2)
            is_tag = True
    else:
        with open('.git/HEAD') as fp:
            head = fp.read()
            if re.search(r'^[0-9a-f]{8,40}$', head):
                revision = head
                ref = local("git branch -r --contains HEAD", capture=True).strip()
                ref = re.search(r'origin/(.*)$', ref).group(1)
            else:
                ref = re.search(r'ref: refs/heads/(.*)', head).group(1)
                revision = local("git rev-parse HEAD", capture=True)
            is_tag = False

    pkg_version = local('python setup.py --version', capture=True)
    if is_tag:
        # it's a tag
        tag = version = ref
        repo = 'latest' if int(tag.split('.')[1]) % 2 else 'stable'
        print_green('tag & version: {0}'.format(tag))
    else:
        # it's a branch
        branch = ref.replace('/', '-').replace('_', '-').replace('.', '')
        env.branch = branch
        version = '{version}.b{build_number}.{revision}'.format(
            version=pkg_version,
            build_number=build_number,
            revision=revision[0:7])
        repo = branch
        print_green('branch: {0}'.format(branch))
        print_green('version: {0}'.format(version))
    print_green('repo: {0}'.format(repo))

    # Load aptly.conf
    for aptly_conf_file in ('/etc/aptly.conf', os.path.expanduser('~/.aptly.conf')):
        if os.path.exists(aptly_conf_file):
            aptly_conf = json.load(open(aptly_conf_file))
            print_green('aptly rootDir: {0}'.format(aptly_conf['rootDir']))
    aptly_prefix = 'release' if is_tag else 'develop'


def import_artifact(src, dst=None):
    '''
    Utility function to import artifacts from Slave
    Example:

        with cd(build_dir):
            run('python setup.py sdist')
            import_artifact('dist/*')
    '''
    print_green('importing artifacts from {0} to {1}'.format(src, artifacts_dir))

    files = get(src, dst or artifacts_dir)
    print_green('imported artifacts:')
    for f in files:
        print_green(os.path.basename(f))


@serial
def git_export():
    '''
    Export current git tree to slave server into the same directory name
    '''
    try:
        host_str = env.host_string.split('@')[1]
    except IndexError:
        host_str = env.host_string
    archive = '{0}-{1}.tar.gz'.format(project, host_str)  # add host str, for safe concurrent execution
    local("git archive --format=tar HEAD | gzip >{0}".format(archive))
    if not os.path.exists(archive):
        f = open(archive, 'w+')
        f.close()
    if '.strider' in build_dir:
        build_dir_pattern = build_dir.rsplit('-', 1)[0] + '-*'
        if os.path.exists(build_dir_pattern):
            run("rm -rf {0}".format(build_dir_pattern))
    elif os.path.exists(build_dir):
        run("rm -rf %s" % build_dir)
    run("mkdir -p %s" % build_dir)

    current_dir = os.path.dirname(os.path.abspath(__file__))
    archive_path = os.path.join(current_dir, archive)
    put(archive_path, build_dir)
    if os.path.exists(archive):
        local('rm -f %s' % archive)
    print_green('exported git tree into %s on slave' % build_dir)
    with cd(build_dir):
        run("tar -xf %s" % archive)


def local_export():
    '''
    Export current working copy to slave server into the same directory
    '''
    archive = '{0}-{1}.tar.gz'.format(project, env.host_string)  # add host str, for safe concurrent execution
    local("tar -czf %s ." % archive)
    run("rm -rf %s" % build_dir)
    run("mkdir -p %s" % build_dir)
    put(archive, build_dir)
    local('rm -f %s' % archive)

    with cd(build_dir):
        run("tar -xf %s" % archive)


def build_omnibus():
    # rm old installation
    print_green('building omnibus')
    with cd(omnibus_dir):
        run("[ -f bin/omnibus ] || bundle install --binstubs")
        env = {
            'BUILD_DIR': build_dir,
            'OMNIBUS_BUILD_VERSION': version,
        }
        with shell_env(**env):
            log_level = 'debug' if verbose else 'info'
            run("bin/omnibus clean %s --log-level=%s" % (project, log_level))
            run("bin/omnibus build %s --log-level=%s" % (project, log_level))

    with open(omnibus_md5sum_file, 'w+') as fp:
        fp.write(omnibus_md5sum())


def build_meta_package(pkg_type, name, version, depends=None):
    with cd('/var/cache/omnibus/pkg'):
        cmd = ('fpm -t {pkg_type} -s empty '
                '--name {name} '
                '--version {version} '
                '--iteration 1 '
                '--maintainer "Scalr Inc. <packages@scalr.net>" '
                '--url "http://scalr.net"').format(
                pkg_type=pkg_type, name=name, version=version)
        if depends:
            cmd += ' --depends "{0}"'.format(depends)
        run(cmd)


def build_meta_packages():
    print_green('building meta packages')
    pkg_type = 'rpm' if 'centos' in env.host_string else 'deb'
    for platform in 'ec2 gce openstack cloudstack ecs idcf ucloud'.split():
        build_meta_package(
                pkg_type,
                'scalarizr-%s' % platform,
                version,
                'scalarizr = %s-1' % version)
    # for scalarizr < 2.x
    build_meta_package(pkg_type, 'scalarizr-base', version)

@task
def build_source():
    '''
    create source distribution
    '''
    init()
    git_export()
    with cd(build_dir):
    # bump project version
        run("echo {0!r} >version".format(version))
        # build project
        run("python setup_agent.py sdist", quiet=True)
        # import tarball
        import_artifact('dist/*.tar.gz')


def bump_version():
    with cd(build_dir):
        run("echo {0!r} >src/scalarizr/version".format(version))


@task
def build_binary():
    '''
    create binary distribution (.deb .rpm)
    '''
    time0 = time.time()
    init()
    git_export()
    bump_version()
    generate_changelog()
    run('rm -rf /var/cache/omnibus/pkg/{0}*'.format(project))
    build_omnibus()
    build_meta_packages()
    import_artifact('/var/cache/omnibus/pkg/{0}*'.format(project))
    time_delta = time.time() - time0
    print_green('build binary took {0}'.format(time_delta))


@task
def build_rpm_deps():
    if os.listdir(rpm_deps_dir):
        return
    run('rm -f /var/cache/omnibus/pkg/yum-*')
    build_meta_package('rpm', 'yum-downloadonly', '0.0.1', 'yum-plugin-downloadonly')
    build_meta_package('rpm', 'yum-plugin-downloadonly', '0.0.1')
    build_meta_package('rpm', 'yum-priorities', '0.0.1')
    local('curl -o %s/scalr-upd-client-0.4.17-1.el6.noarch.rpm '
            'http://rpm.scalr.net/rpm/rhel/6/x86_64/scalr-upd-client-0.4.17-1.el6.noarch.rpm' % rpm_deps_dir)
    import_artifact('/var/cache/omnibus/pkg/yum-*', rpm_deps_dir)
    import_artifact('/var/cache/omnibus/pkg/scalr-*', rpm_deps_dir)



def omnibus_md5sum_changed():
    if not os.path.exists(omnibus_md5sum_file):
        return True

    with open(omnibus_md5sum_file) as fp:
        md5_old = fp.read()
    md5_new = omnibus_md5sum()
    return md5_old != md5_new


def omnibus_md5sum():
    return local("find 'omnibus' -type f | sort | xargs md5sum", capture=True).strip()


def generate_changelog():
    # pylint: disable=W0612,W0621
    template = \
        """{project} ({version}) {branch}; urgency=low

  * Build {project}

 -- {author} <{author_email}>  {now}"""

    project = globals()['project']
    version = globals()['version']
    branch = globals()['branch']
    author = local("git show -s --format=%an", capture=True)
    author_email = local("git show -s --format=%ae", capture=True)
    now = time.strftime("%a, %d %b %Y %H:%M:%S %z", time.gmtime())
    with cd(omnibus_dir):
        run("echo '%s' >changelog" % template.format(**locals()))


@task
@runs_once
def publish_deb():
    '''
    publish .deb packages into local repository
    '''
    time0 = time.time()
    try:
        init()
        if '[%s]' % repo not in local('aptly repo list', capture=True):
            local('aptly repo create -distribution {0} {0}'.format(repo))
        if '[%s]' % repo not in local('aptly publish list', capture=True):
            local(('aptly publish repo -gpg-key={0} '
                    '-architectures i386,amd64 {1} {2}').format(gpg_key, repo, aptly_prefix))
        for pkg_arch in ('i386', 'amd64'):
            # remove previous version
            local('aptly repo remove {0} "Architecture ({1}), Name (~ {2}.*)"'.format(repo, pkg_arch, project))
            # publish artifacts into repo
            packages = glob.glob(artifacts_dir + '/*_{0}.deb'.format(pkg_arch))
            if packages:
                local('aptly repo add {0} {1}'.format(repo, ' '.join(packages)))
        local('aptly publish update -gpg-key={0} {1} {2}'.format(gpg_key, repo, aptly_prefix))
        local('aptly db cleanup')
    finally:
        time_delta = time.time() - time0
        print_green('publish deb took {0}'.format(time_delta))


@task
@runs_once
def publish_deb_plain():
    '''
    publish .deb packages into local repository as a plain debian repo (only for compatibility)
    '''
    init()
    time0 = time.time()
    try:
        with lcd(aptly_conf['rootDir'] + '/public/' + aptly_prefix):
            release_file = 'dists/{0}/Release'.format(repo)
            arches = local('grep Architecture {0}'.format(release_file),
                            capture=True).split(':')[-1].strip().split()
            repo_plain_dir = '{0}/apt-plain/{1}'.format(repo_dir, repo)
            if os.path.exists(repo_plain_dir):
                shutil.rmtree(repo_plain_dir)
            os.makedirs(repo_plain_dir)
            for arch in arches:
                packages_file = 'dists/{0}/main/binary-{1}/Packages'.format(repo, arch)
                # Copy packages
                local(("grep Filename %s | "
                        "awk '{ print $2 }' | "
                        "xargs -I '{}' cp '{}' %s/") % (packages_file, repo_plain_dir))

        with lcd(os.path.dirname(repo_plain_dir)):
            local('dpkg-scanpackages -m {0} > {0}/Packages'.format(repo))
            local('dpkg-scansources {0} > {0}/Sources'.format(repo))
            with lcd(repo):
                with open('{0}/Release'.format(repo_plain_dir), 'w+') as fp:
                    distribution = 'scalr' if tag else repo
                    fp.write((
                        'Origin: scalr\n'
                        'Label: {0}\n'
                        'Codename: {0}\n'
                        'Architectures: all {1}\n'
                        'Description: Scalr packages\n'
                    ).format(distribution, ' '.join(arches)))
                    fp.write(local('apt-ftparchive release .', capture=True))
                local('cat Packages | gzip -9c > Packages.gz')
                local('cat Sources | gzip -9c > Sources.gz')
                local('gpg -v --clearsign -u {0} -o InRelease Release'.format(gpg_key))
                local('gpg -v -abs -u {0} -o Release.gpg Release'.format(gpg_key))
    finally:
        time_delta = time.time() - time0
        print_green('publish plain deb repository took {0}'.format(time_delta))


@task
@runs_once
def publish_rpm():
    '''
    publish .rpm packages into local repository.
    '''
    init()
    time0 = time.time()
    try:
        repo_path = '%s/rpm/%s/rhel' % (repo_dir, repo)

        # create directory structure
        local('mkdir -p %s/5/{x86_64,i386}' % repo_path, shell='/bin/bash')
        local('mkdir -p %s/{6,7}' % repo_path, shell='/bin/bash')
        cwd = os.getcwd()
        os.chdir(repo_path)

        def symlink(target, linkname):
            if not os.path.exists(linkname):
                os.symlink(target, linkname)
        for linkname in '5Server'.split():
            symlink('5', linkname)
        for linkname in '6Server 6.0 6.1 6.2 6.3 6.4 6.5'.split():
            symlink('6', linkname)
        for linkname in '2013.03 2013.09 2014.03 2014.09 latest'.split():
            symlink('6', linkname)
        for linkname in '7Server 7.0'.split():
            symlink('7', linkname)
        # Symlink el6 and el7 package directories to el5
        for arch in ('i386', 'x86_64'):
            for ver in '6 7'.split():
                symlink('../5/%s' % arch, '%s/%s' % (ver, arch))

        os.chdir(cwd)

        # remove previous version
        local('rm -f %s/*/*/%s*.rpm' % (repo_path, project))

        # publish artifacts into repo
        for arch, pkg_arch in (('i386', 'i686'), ('x86_64', 'x86_64')):
            ver = '5'
            dst = os.path.join(repo_path, ver, arch)
            local('cp %s/%s*%s.rpm %s/' % (artifacts_dir, project, pkg_arch, dst))
            local('cp %s/*%s.rpm -u %s/' % (rpm_deps_dir, pkg_arch, dst))
            #local('cp %s/*noarch.rpm -u %s/' % (rpm_deps_dir, dst))
            local('createrepo %s' % dst)

    finally:
        time_delta = time.time() - time0
        print_green('publish rpm took {0}'.format(time_delta))


@task
@runs_once
def publish_win():
    '''
    publish .msi packages into local repository.
    '''
    init()
    time0 = time.time()
    try:
        repo_path = '%s/win/%s' % (repo_dir, repo)
        local("mkdir -p %s" % repo_path)
    finally:
        time_delta = time.time() - time0
        print_green('publish win took {0}'.format(time_delta))


def cleanup_artifacts():
    print_green('Running cleanup task in {0}'.format(project_dir))
    artifact_dirs = sorted(glob.glob('{0}/*'.format(project_dir)))
    num_artifacts = len(artifact_dirs)
    if num_artifacts > permitted_artifacts_number:
        print_green(
            'Artifact number exeeding permitted value.'
            'Removing {0} artifact directories'.format(num_artifacts - permitted_artifacts_number))

        for directory in artifact_dirs[0:-permitted_artifacts_number]:
            if os.path.isdir(directory):
                local('rm -rf {0}'.format(directory))


@task
@runs_once
def release():
    '''
    sync packages from local repository to Scalr.net
    '''
    init()

    rsync_cmd = "rsync -av --rsh 'ssh -l {0} -p {1}' ".format(remote_repo_user, remote_repo_port)
    rsync_cmd += "{include} {exclude} {src} " + '{0}'.format(remote_repo_host) + ':{dest}'

    # Sync rpm, apt(plain) and win repos
    includes = ('rpm/', 'rpm/latest**', 'rpm/stable**',
                'apt-plain/', 'apt-plain/latest**', 'apt-plain/stable**')
    #            'win/', 'win/latest**', 'win/stable**')
    includes_cmd = []
    for include in includes:
        includes_cmd.append('--include')
        includes_cmd.append(repr(include))
    includes_cmd = ' '.join(includes_cmd)
    local(rsync_cmd.format(
            include=includes_cmd,
            exclude="--exclude '*'",
            src=repo_dir + '/',
            dest=remote_repo_dir))

    # Sync apt(pool)
    local(rsync_cmd.format(
            include='',
            exclude='',
            src=repo_dir + '/apt/release/',
            dest=remote_repo_dir + '/apt'))


@task
@runs_once
def publish_binary():
    '''
    publish all packages into local repository
    '''
    publish_rpm()
    publish_deb()
    publish_deb_plain()
    publish_win()
    if tag:
        release()


@task
def cleanup():
    run('rm -rf /root/.strider/data/scalr-int-scalarizr-*')
    # additional cleanup for cases when user was previously defined incorrectly
    run('rm -rf /.strider/data/scalr-int-scalarizr-*')
    run('find /tmp -mindepth 1 -maxdepth 1 ! -name "vagrant-chef-*" | xargs rm -rf')


def print_green(msg):
    print green('[localhost] {0}'.format(msg))


def print_red(msg):
    print red('[localhost] {0}'.format(msg))


def print_yellow(msg):
    print yellow('[localhost] {0}'.format(msg))
