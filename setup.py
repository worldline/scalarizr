import os
from setuptools import setup, findall, find_packages
from distutils import sysconfig
from distutils.util import change_root
from distutils.command.install_data import install_data


class my_install_data(install_data):
	def run(self):
		install_data.run(self)
		
		# Install scripts
		shbang = "#!" + os.path.join(
			sysconfig.get_config_var("BINDIR"), 
			"python%s%s" % (sysconfig.get_config_var("VERSION"), sysconfig.get_config_var("EXE"))
		)
		
		entries = list(t for t in self.data_files if t[0].startswith("/usr"))
		for ent in entries:
			dir = change_root(self.root, ent[0])			
			for file in ent[1]:
				path = os.path.join(dir, os.path.basename(file))
				f = None
				try:
					f = open(path, "r")
					script = f.readline()
					script = script.replace("#!/usr/bin/python", shbang)
					script += f.read()
				finally:
					f.close()
					
				try:
					f = open(path, "w")
					f.write(script)
				finally:
					f.close()


def make_data_files(dst, src):
	ret = []
	for dir, dirname, files in os.walk(src):
		 if dir.find(".svn") == -1:
		 	ret.append([
				dir.replace(src, dst),
				list(os.path.join(dir, f) for f in files)
			])
	return ret

description = "Scalarizr converts any server to Scalr-manageable node"


data_files = make_data_files("/etc/scalr", "etc")
data_files.extend(make_data_files("/usr/local/scalarizr/scripts", "scripts"))
data_files.append(["/usr/local/bin", ["bin/scalarizr"]])


cfg = dict(
	name = "scalarizr",
	version = "0.5",	 
	description = description,
	long_description = description,
	author = "Scalr Inc.",
	author_email = "info@scalr.net",
	url = "https://scalr.net",
	license = "GPL",
	platforms = "any",
	package_dir = {"" : "src"},
	packages = find_packages("src"),
	requires = ["m2crypto (>=0.20)", "boto"],
	data_files = data_files,
	cmdclass={"install_data" : my_install_data}
)
setup(**cfg)


