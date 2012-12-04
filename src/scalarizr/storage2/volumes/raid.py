from __future__ import with_statement

__author__ = 'Nick Demyanchuk'

import os
import re
import sys
import Queue
import base64
import logging
import tempfile
import threading
import itertools


from scalarizr import storage2, util
from scalarizr.linux import mdadm, lvm2, coreutils
from scalarizr.storage2.volumes import base


LOG = logging.getLogger(__name__)


class RaidVolume(base.Volume):


	lv_re = re.compile(r'Logical volume "([^\"]+)" created')

	
	def __init__(self, 
				disks=None, raid_pv=None, level=None, lvm_group_cfg=None, 
				vg=None, pv_uuid=None, **kwds):
		'''
		:type disks: list
		:param disks: Raid disks

		:type raid_pv: string
		:param raid_pv: Raid device name (e.g: /dev/md0)

		:type level: int
		:param level: Raid level. Valid values are
			* 0
			* 1
			* 5
			* 10

		:type lvm_group_cfg: string
		:param lvm_group_cfg: LVM volume group configuration (base64 encoded)

		:type vg: string
		:param vg: LVM volume group name

		:type pv_uuid: string
		:param pv_uuid: Mdadm device physical volume id
		'''
		# Backward compatibility with old storage
		if vg is not None:
			vg = os.path.basename(vg)

		super(RaidVolume, self).__init__(disks=disks or [],
				raid_pv=raid_pv, level=level and int(level), 
				lvm_group_cfg=lvm_group_cfg,
				vg=vg, pv_uuid=pv_uuid, **kwds)
		self.features.update({'restore': True, 'grow': True})


	def _ensure(self):
		if self.snap:
			disks = []
			try:
				for disk_snap in self.snap['disks']:
					snap = storage2.snapshot(disk_snap)
					disks.append(snap.restore())
			except:
				for disk in disks:
					disk.destroy()
				raise

			self.disks = disks

			self.vg = self.snap['vg']
			self.level = int(self.snap['level'])
			self.pv_uuid = self.snap['pv_uuid']
			self.lvm_group_cfg = self.snap['lvm_group_cfg']

			self.snap = None

		self._check_attr('level')
		self._check_attr('vg')
		self._check_attr('disks')

		assert int(self.level) in (0,1,5,10),\
									'Unknown raid level: %s' % self.level

		disks = []
		for disk in self.disks:
			disk = storage2.volume(disk)
			disk.ensure()
			disks.append(disk)
		self.disks = disks

		disks_devices = [disk.device for disk in self.disks]
		vg_name = os.path.basename(self.vg)

		if self.lvm_group_cfg:
			try:
				raid_device = mdadm.mdfind(*disks_devices)
			except storage2.StorageError:
				raid_device = mdadm.findname()
				"""
				if self.level in (1, 10):
					for disk in disks_devices:
						mdadm.mdadm('misc', None, disk,
									zero_superblock=True, force=True)

					kwargs = dict(force=True, metadata='default',
								  level=self.level, assume_clean=True,
								  raid_devices=len(disks_devices))
					mdadm.mdadm('create', raid_device, *disks_devices, **kwargs)
				else:
				"""
				mdadm.mdadm('assemble', raid_device, *disks_devices)
				mdadm.mdadm('misc', None, raid_device, wait=True, raise_exc=False)

			try:
				lvm2.pvs(raid_device)
			except:
				lvm2.pvcreate(raid_device, uuid=self.pv_uuid)

			# Restore vg
			tmpfile = tempfile.mktemp()
			try:
				with open(tmpfile, 'w') as f:
					f.write(base64.b64decode(self.lvm_group_cfg))
				lvm2.vgcfgrestore(vg_name, file=tmpfile)
			finally:
				os.remove(tmpfile)

			# Check that logical volume exists
			lv_infos = lvm2.lvs(self.vg)
			if not lv_infos:
				raise storage2.StorageError(
					'No logical volumes found in %s vol. group')
			lv_name = lv_infos.popitem()[1].lv_name
			self.device = lvm2.lvpath(self.vg, lv_name)

			# Activate volume group
			lvm2.vgchange(vg_name, available='y')

			# Wait for logical volume device file
			util.wait_until(lambda: os.path.exists(self.device),
						timeout=120, logger=LOG,
						error_text='Logical volume %s not found' % self.device)

		else:
			raid_device = mdadm.findname()
			kwargs = dict(force=True, level=self.level, assume_clean=True,
						  raid_devices=len(disks_devices), metadata='default')
			mdadm.mdadm('create', raid_device, *disks_devices, **kwargs)
			mdadm.mdadm('misc', None, raid_device, wait=True, raise_exc=False)

			lvm2.pvcreate(raid_device, force=True)
			self.pv_uuid = lvm2.pvs(raid_device)[raid_device].pv_uuid

			lvm2.vgcreate(vg_name, raid_device)

			out, err = lvm2.lvcreate(vg_name, extents='100%FREE')[:2]
			try:
				clean_out = out.strip().split('\n')[-1].strip()
				vol = re.match(self.lv_re, clean_out).group(1)
				self.device = lvm2.lvpath(vg_name, vol)
			except:
				e = 'Logical volume creation failed: %s\n%s' % (out, err)
				raise Exception(e)

			self.lvm_group_cfg = lvm2.backup_vg_config(vg_name)

		self.raid_pv = raid_device


	def _detach(self, force, **kwds):
		self.lvm_group_cfg = lvm2.backup_vg_config(self.vg)
		lvm2.vgremove(self.vg, force=True)
		lvm2.pvremove(self.raid_pv, force=True)

		mdadm.mdadm('misc', None, self.raid_pv, stop=True, force=True)
		try:
			mdadm.mdadm('manage', None, self.raid_pv, remove=True, force=True)
		except (Exception, BaseException), e:
			if not 'No such file or directory' in str(e):
				raise

		try:
			os.remove(self.raid_pv)
		except:
			pass

		self.raid_pv = None

		for disk in self.disks:
			disk.detach(force=force)

		self.device = None


	def _snapshot(self, description, tags, **kwds):
		coreutils.sync()
		lvm2.dmsetup('suspend', self.device)
		try:
			descr = 'Raid%s disk ${index}. %s' % (self.level, description or '')
			disks_snaps = storage2.concurrent_snapshot(
				volumes=self.disks,
				description=descr,
				tags=tags, **kwds
			)

			return storage2.snapshot(
				type='raid',
				disks=disks_snaps,
				lvm_group_cfg=lvm2.backup_vg_config(self.vg),
				level=self.level,
				pv_uuid=self.pv_uuid,
				vg=self.vg
			)
		finally:
			lvm2.dmsetup('resume', self.device)


	def _destroy(self, force, **kwds):
		remove_disks = kwds.get('remove_disks')
		if remove_disks:
			for disk in self.disks:
				disk.destroy(force=force)
			self.disks = []


	def _clone(self, config):
		disks = []
		for disk_cfg_or_obj in self.disks:
			disk = storage2.volume(disk_cfg_or_obj)
			disk_clone = disk.clone()
			disks.append(disk_clone)

		config['disks'] = disks
		for attr in ('pv_uuid', 'lvm_group_cfg', 'raid_pv', 'device'):
			config.pop(attr, None)


	def check_growth_cfg(self, **growth_cfg):
		if int(self.level) in (0, 10):
			raise storage2.StorageError("Raid%s doesn't support growth" % self.level)

		foreach_cfg = growth_cfg.get('foreach')
		change_disks = False

		if foreach_cfg:
			for disk_cfg_or_obj in self.disks:
				disk = storage2.volume(disk_cfg_or_obj)
				try:
					disk.check_growth_cfg(**foreach_cfg)
					change_disks = True
				except storage2.NoOpError:
					pass

		new_len = growth_cfg.get('len')
		current_len = len(self.disks)
		change_size = new_len and int(new_len) != current_len

		if not change_size and not change_disks:
			raise storage2.NoOpError('Configurations are equal. Nothing to do')

		if change_size and int(new_len) < current_len:
			raise storage2.StorageError('Disk count can only be increased.')

		if change_size and int(self.level) in (0, 10):
			raise storage2.StorageError("Can't add disks to raid level %s"
																% self.level)

	def _grow(self, new_vol, **growth_cfg):
		if int(self.level) in (0, 10):
			raise storage2.StorageError("Raid%s doesn't support growth" % self.level)
			
		foreach_cfg = growth_cfg.get('foreach')

		current_len = len(self.disks)
		new_len = int(growth_cfg.get('len', 0))
		increase_disk_count = new_len and new_len != current_len

		new_vol.lvm_group_cfg = self.lvm_group_cfg
		new_vol.pv_uuid = self.pv_uuid

		growed_disks = []
		added_disks = []
		try:
			if foreach_cfg:

				def _grow(index, disk, cfg, queue):
					try:
						ret = disk.grow(resize_fs=False, **cfg)
						queue.put(dict(index=index, result=ret))
					except:
						e = sys.exc_info()[1]
						queue.put(dict(index=index, error=e))

				# Concurrently grow each descendant disk
				queue = Queue.Queue()
				pool = []
				for index, disk_cfg_or_obj in enumerate(self.disks):
					# We use index to save disk order in raid disks
					disk = storage2.volume(disk_cfg_or_obj)

					t = threading.Thread(
						name='Raid %s disk %s grower' %	(self.id, disk.id),
						target=_grow, args=(index, disk, foreach_cfg, queue))
					t.daemon = True
					t.start()
					pool.append(t)

				for thread in pool:
					thread.join()

				# Get disks growth results
				res = []
				while True:
					try:
						res.append(queue.get_nowait())
					except Queue.Empty:
						break

				res.sort(key=lambda p: p['index'])
				growed_disks = [r['result'] for r in res if 'result' in r]

				# Validate concurrent growth results
				assert len(res) == len(self.disks), ("Not enough data in "
						"concurrent raid disks grow result")

				if not all(map(lambda x: 'result' in x, res)):
					errors = '\n'.join([str(r['error']) for r in res if 'error' in r])
					raise storage2.StorageError('Failed to grow raid disks.'
							' Errors: \n%s' % errors)

				assert len(growed_disks) == len(self.disks), ("Got malformed disks"
							" growth result (not enough data).")

				new_vol.disks = growed_disks
				new_vol.pv_uuid = self.pv_uuid
				new_vol.lvm_group_cfg = self.lvm_group_cfg

				new_vol.ensure()

			if increase_disk_count:
				if not foreach_cfg:
					""" It means we have original disks in self.disks
						We need to snapshot it and make new disks.
					"""
					new_vol.disks = []
					snaps = storage2.concurrent_snapshot(self.disks,
							'Raid %s temp snapshot No.${index} (for growth)' % self.id,
							tags=dict(temp='1'))
					try:
						for disk, snap in zip(self.disks, snaps):
							new_disk = disk.clone()
							new_disk.snap = snap
							new_vol.disks.append(new_disk)
							new_disk.ensure()
					finally:
						for s in snaps:
							try:
								s.destroy()
							except:
								e = sys.exc_info()[1]
								LOG.debug('Failed to remove temporary snapshot: %s' % e)

					new_vol.ensure()

				existing_raid_disk = new_vol.disks[0]
				add_disks_count = new_len - current_len
				for _ in range(add_disks_count):
					disk_to_add = existing_raid_disk.clone()
					added_disks.append(disk_to_add)
					disk_to_add.ensure()

				added_disks_devices = [d.device for d in added_disks]
				mdadm.mdadm('manage', new_vol.raid_pv, add=True,
														*added_disks_devices)
				new_vol.disks.extend(added_disks)

				mdadm.mdadm('grow', new_vol.raid_pv, raid_devices=new_len)

			mdadm.mdadm('misc', None, new_vol.raid_pv, wait=True, raise_exc=False)
			mdadm.mdadm('grow', new_vol.raid_pv, size='max')
			mdadm.mdadm('misc', None, new_vol.raid_pv, wait=True, raise_exc=False)

			lvm2.pvresize(new_vol.raid_pv)
			try:
				lvm2.lvresize(new_vol.device, extents='100%VG')
			except:
				e = sys.exc_info()[1]
				if (self.level == 1 and 'matches existing size' in str(e)
														and not foreach_cfg):
					LOG.debug('Raid1 actual size has not changed')
				else:
					raise
		except:
			err_type, err_val, trace = sys.exc_info()
			if growed_disks or added_disks:
				LOG.debug("Removing %s successfully growed disks and "
							"%s additional disks",
						  	len(growed_disks), len(added_disks))
				for disk in itertools.chain(growed_disks, added_disks):
					try:
						disk.destroy(force=True)
					except:
						e = sys.exc_info()[1]
						LOG.error('Failed to remove raid disk: %s' % e)

			raise err_type, err_val, trace


class RaidSnapshot(base.Snapshot):

	def __init__(self, **kwds):
		super(RaidSnapshot, self).__init__(**kwds)
		self.disks = map(storage2.snapshot, self.disks)


	def _destroy(self):
		for disk in self.disks:
			disk.destroy()


	def _status(self):
		if all((snap.status() == self.COMPLETED for snap in self.disks)):
			return self.COMPLETED
		elif any((snap.status() == self.FAILED for snap in self.disks)):
			return self.FAILED
		elif any((snap.status() == self.IN_PROGRESS for snap in self.disks)):
			return self.IN_PROGRESS
		return self.UNKNOWN


storage2.volume_types['raid'] = RaidVolume
storage2.snapshot_types['raid'] = RaidSnapshot
