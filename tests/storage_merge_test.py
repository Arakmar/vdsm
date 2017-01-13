#
# Copyright 2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
from contextlib import contextmanager
from collections import namedtuple
from functools import partial

from monkeypatch import MonkeyPatchScope
from storagefakelib import FakeResourceManager
from storagetestlib import fake_env
from storagefakelib import fake_guarded_context
from storagetestlib import make_qemu_chain
from testValidation import brokentest
from testlib import make_uuid
from testlib import expandPermutations, permutations
from testlib import VdsmTestCase

from vdsm import qemuimg
from vdsm.storage import constants as sc
from vdsm.storage import exception as se
from vdsm.storage import guarded
from vdsm.storage import resourceManager

from storage import blockVolume
from storage import fileVolume
from storage import merge
from storage import image
from storage import sd
from storage import volume

MB = 1024 ** 2
GB = 1024 ** 3


# XXX: Ideally we wouldn't fake these methods but the originals are defined in
# the Volume class and use SPM rollbacks so we cannot use them.
def fake_blockVolume_extendSize(env, vol_instance, new_size_blk):
    new_size = new_size_blk * sc.BLOCK_SIZE
    new_size_mb = (new_size + MB - 1) / MB
    env.lvm.extendLV(env.sd_manifest.sdUUID, vol_instance.volUUID, new_size_mb)
    vol_instance.setSize(new_size_blk)


def fake_fileVolume_extendSize(env, vol_instance, new_size_blk):
    new_size = new_size_blk * sc.BLOCK_SIZE
    vol_path = vol_instance.getVolumePath()
    env.sd_manifest.oop.truncateFile(vol_path, new_size)
    vol_instance.setSize(new_size_blk)


Volume = namedtuple("Volume", "format,virtual,physical")
Expected = namedtuple("Expected", "virtual,physical")


@contextmanager
def make_env(env_type, base, top):
    img_id = make_uuid()
    base_id = make_uuid()
    top_id = make_uuid()

    if env_type == 'block' and base.format == 'raw':
        prealloc = sc.PREALLOCATED_VOL
    else:
        prealloc = sc.SPARSE_VOL

    with fake_env(env_type) as env:
        env.make_volume(base.virtual * GB, img_id, base_id,
                        vol_format=sc.name2type(base.format),
                        prealloc=prealloc)
        env.make_volume(top.virtual * GB, img_id, top_id,
                        parent_vol_id=base_id,
                        vol_format=sc.COW_FORMAT)
        env.subchain = merge.SubchainInfo(
            dict(sd_id=env.sd_manifest.sdUUID, img_id=img_id,
                 base_id=base_id, top_id=top_id), 0)

        if env_type == 'block':
            # Simulate allocation by adjusting the LV sizes
            env.lvm.extendLV(env.sd_manifest.sdUUID, base_id,
                             base.physical * GB / MB)
            env.lvm.extendLV(env.sd_manifest.sdUUID, top_id,
                             top.physical * GB / MB)

        rm = FakeResourceManager()
        with MonkeyPatchScope([
            (guarded, 'context', fake_guarded_context()),
            (merge, 'sdCache', env.sdcache),
            (blockVolume, 'rm', rm),
            (blockVolume, 'sdCache', env.sdcache),
            (image.Image, 'getChain', lambda self, sdUUID, imgUUID:
                [env.subchain.base_vol, env.subchain.top_vol]),
            (blockVolume.BlockVolume, 'extendSize',
                partial(fake_blockVolume_extendSize, env)),
            (fileVolume.FileVolume, 'extendSize',
                partial(fake_fileVolume_extendSize, env)),
        ]):
            yield env


class FakeImage(object):

    def __init__(self, repoPath):
        pass


@expandPermutations
class TestSubchainInfo(VdsmTestCase):

    # TODO: use one make_env for all tests?
    @contextmanager
    def make_env(self, sd_type='file', format='raw', chain_len=2,
                 shared=False):
        size = 1048576
        base_fmt = sc.name2type(format)
        with fake_env(sd_type) as env:
            rm = FakeResourceManager()
            with MonkeyPatchScope([
                (guarded, 'context', fake_guarded_context()),
                (merge, 'sdCache', env.sdcache),
                (blockVolume, 'rm', rm),
            ]):
                env.chain = make_qemu_chain(env, size, base_fmt, chain_len)

                def fake_chain(self, sdUUID, imgUUID, volUUID=None):
                    return env.chain

                image.Image.getChain = fake_chain

                yield env

    def test_legal_chain(self):
        with self.make_env() as env:
            base_vol = env.chain[0]
            top_vol = env.chain[1]
            subchain_info = dict(sd_id=base_vol.sdUUID,
                                 img_id=base_vol.imgUUID,
                                 base_id=base_vol.volUUID,
                                 top_id=top_vol.volUUID,
                                 base_generation=0)

            subchain = merge.SubchainInfo(subchain_info, 0)
            # Next subchain.validate() should pass without exceptions
            subchain.validate()

    def test_validate_base_is_not_in_chain(self):
        with self.make_env() as env:
            top_vol = env.chain[1]
            subchain_info = dict(sd_id=top_vol.sdUUID,
                                 img_id=top_vol.imgUUID,
                                 base_id=make_uuid(),
                                 top_id=top_vol.volUUID,
                                 base_generation=0)

            subchain = merge.SubchainInfo(subchain_info, 0)
            self.assertRaises(se.VolumeIsNotInChain, subchain.validate)

    def test_validate_top_is_not_in_chain(self):
        with self.make_env() as env:
            base_vol = env.chain[0]
            subchain_info = dict(sd_id=base_vol.sdUUID,
                                 img_id=base_vol.imgUUID,
                                 base_id=base_vol.volUUID,
                                 top_id=make_uuid(),
                                 base_generation=0)

            subchain = merge.SubchainInfo(subchain_info, 0)
            self.assertRaises(se.VolumeIsNotInChain, subchain.validate)

    def test_validate_vol_is_not_base_parent(self):
        with self.make_env(chain_len=3) as env:
            base_vol = env.chain[0]
            top_vol = env.chain[2]
            subchain_info = dict(sd_id=top_vol.sdUUID,
                                 img_id=top_vol.imgUUID,
                                 base_id=base_vol.volUUID,
                                 top_id=top_vol.volUUID,
                                 base_generation=0)

            subchain = merge.SubchainInfo(subchain_info, 0)
            self.assertRaises(se.WrongParentVolume, subchain.validate)

    @permutations((
        # shared volume
        (0,),
        (1,),
    ))
    def test_validate_vol_is_not_shared(self, shared_vol):
        with self.make_env(chain_len=3, shared=True) as env:
            base_vol = env.chain[0]
            top_vol = env.chain[1]
            env.chain[shared_vol].setShared()
            subchain_info = dict(sd_id=top_vol.sdUUID,
                                 img_id=top_vol.imgUUID,
                                 base_id=base_vol.volUUID,
                                 top_id=top_vol.volUUID,
                                 base_generation=0)

            subchain = merge.SubchainInfo(subchain_info, 0)
            self.assertRaises(se.SharedVolumeNonWritable, subchain.validate)


@expandPermutations
class TestPrepareMerge(VdsmTestCase):

    @permutations((
        # No capacity update, no allocation update
        (Volume('raw', 1, 1), Volume('cow', 1, 1), Expected(1, 1)),
        # No capacity update, increase LV size
        (Volume('cow', 10, 2), Volume('cow', 10, 2), Expected(10, 5)),
        # Update capacity and increase LV size
        (Volume('cow', 3, 1), Volume('cow', 5, 1), Expected(5, 3)),
    ))
    def test_block_cow(self, base, top, expected):
        with make_env('block', base, top) as env:
            merge.prepare(env.subchain)
            self.assertEqual(sorted(self.expected_locks(env.subchain)),
                             sorted(guarded.context.locks))
            base_vol = env.subchain.base_vol
            self.assertEqual(sc.ILLEGAL_VOL, base_vol.getLegality())
            new_base_size = base_vol.getSize() * sc.BLOCK_SIZE
            new_base_alloc = env.sd_manifest.getVSize(base_vol.imgUUID,
                                                      base_vol.volUUID)
            self.assertEqual(expected.virtual * GB, new_base_size)
            self.assertEqual(expected.physical * GB, new_base_alloc)

    @brokentest("Looks like it is impossible to create a domain object in "
                "the tests")
    @permutations((
        # Update capacity and fully allocate LV
        (Volume('raw', 1, 1), Volume('cow', 2, 1), Expected(2, 2)),
    ))
    def test_block_raw(self, base, top, expected):
        with make_env('block', base, top) as env:
            merge.prepare(env.subchain)
            self.assertEqual(sorted(self.expected_locks(env.subchain)),
                             sorted(guarded.context.locks))
            base_vol = env.subchain.base_vol
            self.assertEqual(sc.ILLEGAL_VOL, base_vol.getLegality())
            new_base_size = base_vol.getSize() * sc.BLOCK_SIZE
            new_base_alloc = env.sd_manifest.getVSize(base_vol.imgUUID,
                                                      base_vol.volUUID)
            self.assertEqual(expected.virtual * GB, new_base_size)
            self.assertEqual(expected.physical * GB, new_base_alloc)

    @permutations((
        (Volume('cow', 1, 0), Volume('cow', 1, 0), Expected(1, 0)),
        (Volume('cow', 1, 0), Volume('cow', 2, 0), Expected(2, 0)),
    ))
    def test_file_cow(self, base, top, expected):
        with make_env('file', base, top) as env:
            merge.prepare(env.subchain)
            base_vol = env.subchain.base_vol
            self.assertEqual(sc.ILLEGAL_VOL, base_vol.getLegality())
            new_base_size = base_vol.getSize() * sc.BLOCK_SIZE
            self.assertEqual(expected.virtual * GB, new_base_size)

    @brokentest("Looks like it is impossible to create a domain object in "
                "the tests")
    @permutations((
        (Volume('raw', 1, 0), Volume('cow', 2, 0), Expected(2, 0)),
    ))
    def test_file_raw(self, base, top, expected):
        with make_env('file', base, top) as env:
            merge.prepare(env.subchain)
            base_vol = env.subchain.base_vol
            self.assertEqual(sc.ILLEGAL_VOL, base_vol.getLegality())
            new_base_size = base_vol.getSize() * sc.BLOCK_SIZE
            self.assertEqual(expected.virtual * GB, new_base_size)

    def expected_locks(self, subchain):
        img_ns = sd.getNamespace(sc.IMAGE_NAMESPACE, subchain.sd_id)
        return [
            resourceManager.ResourceManagerLock(sc.STORAGE, subchain.sd_id,
                                                resourceManager.SHARED),
            resourceManager.ResourceManagerLock(img_ns, subchain.img_id,
                                                resourceManager.EXCLUSIVE),
            volume.VolumeLease(subchain.host_id, subchain.sd_id,
                               subchain.img_id, subchain.base_id)
        ]


class FakeSyncVolumeChain(object):

    def __call__(self, sd_id, img_id, vol_id, actual_chain):
        self.sd_id = sd_id
        self.img_id = img_id
        self.vol_id = vol_id
        self.actual_chain = actual_chain


@expandPermutations
class TestFinalizeMerge(VdsmTestCase):

    # TODO: use one make_env for all tests?
    @contextmanager
    def make_env(self, sd_type='block', format='raw', chain_len=2):
        size = 1048576
        base_fmt = sc.name2type(format)
        with fake_env(sd_type) as env:
            rm = FakeResourceManager()
            with MonkeyPatchScope([
                (guarded, 'context', fake_guarded_context()),
                (merge, 'sdCache', env.sdcache),
                (blockVolume, 'rm', rm),
                (image, 'Image', FakeImage),
            ]):
                env.chain = make_qemu_chain(env, size, base_fmt, chain_len)

                def fake_chain(self, sdUUID, imgUUID, volUUID=None):
                    return env.chain

                image.Image.getChain = fake_chain
                image.Image.syncVolumeChain = FakeSyncVolumeChain()

                yield env

    def test_validate_illegal_base(self):
        with self.make_env(sd_type='file', chain_len=3) as env:
            base_vol = env.chain[0]
            # This volume was *not* prepared
            base_vol.setLegality(sc.LEGAL_VOL)
            top_vol = env.chain[1]

            subchain_info = dict(sd_id=base_vol.sdUUID,
                                 img_id=base_vol.imgUUID,
                                 base_id=base_vol.volUUID,
                                 top_id=top_vol.volUUID,
                                 base_generation=0)

            subchain = merge.SubchainInfo(subchain_info, 0)
            with self.assertRaises(se.UnexpectedVolumeState):
                merge.finalize(subchain)

    @permutations([
        # sd_type, chain_len, base_index, top_index
        ('file', 2, 0, 1),
        ('block', 2, 0, 1),
        ('file', 4, 1, 2),
        ('block', 4, 1, 2),
    ])
    def test_finalize(self, sd_type, chain_len, base_index, top_index):
        with self.make_env(sd_type=sd_type, chain_len=chain_len) as env:
            base_vol = env.chain[base_index]
            # This volume *was* prepared
            base_vol.setLegality(sc.ILLEGAL_VOL)

            top_vol = env.chain[top_index]
            subchain_info = dict(sd_id=base_vol.sdUUID,
                                 img_id=base_vol.imgUUID,
                                 base_id=base_vol.volUUID,
                                 top_id=top_vol.volUUID,
                                 base_generation=0)
            subchain = merge.SubchainInfo(subchain_info, 0)

            merge.finalize(subchain)

            # If top has a child, the child must now be rebased on base.
            if top_vol is not env.chain[-1]:
                child_vol = env.chain[top_index + 1]
                info = qemuimg.info(child_vol.volumePath)
                self.assertEqual(info['backingfile'], base_vol.volumePath)

            # verify syncVolumeChain arguments
            self.assertEquals(image.Image.syncVolumeChain.sd_id,
                              subchain.sd_id)
            self.assertEquals(image.Image.syncVolumeChain.img_id,
                              subchain.img_id)
            self.assertEquals(image.Image.syncVolumeChain.vol_id,
                              env.chain[-1].volUUID)
            new_chain = [vol.volUUID for vol in env.chain]
            new_chain.remove(top_vol.volUUID)
            self.assertEquals(image.Image.syncVolumeChain.actual_chain,
                              new_chain)

            self.assertEqual(base_vol.getLegality(), sc.LEGAL_VOL)