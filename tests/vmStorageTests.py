#
# Copyright 2015 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import

import xml.etree.cElementTree as etree

from monkeypatch import MonkeyPatch
from testlib import VdsmTestCase
from testlib import XMLTestCase
from testlib import make_config
from testlib import permutations, expandPermutations

from vdsm import constants
from vdsm import utils

from virt import vmxml
from virt.vmdevices import storage
from virt.vmdevices.storage import Drive, DISK_TYPE, DRIVE_SHARED_TYPE


class DriveXMLTests(XMLTestCase):

    def test_cdrom(self):
        conf = drive_config(
            device='cdrom',
            iface='ide',
            index='2',
            path='/path/to/fedora.iso',
            readonly='True',
            serial='54-a672-23e5b495a9ea',
        )
        xml = """
            <disk device="cdrom" snapshot="no" type="file">
                <source file="/path/to/fedora.iso" startupPolicy="optional"/>
                <target bus="ide" dev="hdc"/>
                <readonly/>
                <serial>54-a672-23e5b495a9ea</serial>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=False)

    def test_disk_virtio_cache(self):
        conf = drive_config(
            format='cow',
            propagateErrors='on',
            serial='54-a672-23e5b495a9ea',
            shared='shared',
            specParams={
                'ioTune': {
                    'read_bytes_sec': 6120000,
                    'total_iops_sec': 800,
                }
            },
        )
        xml = """
            <disk device="disk" snapshot="no" type="file">
                <source file="/path/to/volume"/>
                <target bus="virtio" dev="vda"/>
                <shareable/>
                <serial>54-a672-23e5b495a9ea</serial>
                <driver cache="writethrough" error_policy="enospace"
                        io="threads" name="qemu" type="qcow2"/>
                <iotune>
                    <read_bytes_sec>6120000</read_bytes_sec>
                    <total_iops_sec>800</total_iops_sec>
                </iotune>
            </disk>
            """
        vm_conf = {'custom': {'viodiskcache': 'writethrough'}}
        self.check(vm_conf, conf, xml, is_block_device=False)

    def test_disk_block(self):
        conf = drive_config(
            serial='54-a672-23e5b495a9ea',
        )
        xml = """
            <disk device="disk" snapshot="no" type="block">
                <source dev="/path/to/volume"/>
                <target bus="virtio" dev="vda"/>
                <serial>54-a672-23e5b495a9ea</serial>
                <driver cache="none" error_policy="stop"
                        io="native" name="qemu" type="raw"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=True)

    def test_disk_with_discard_on(self):
        conf = drive_config(
            serial='54-a672-23e5b495a9ea',
            discard=True,
        )
        xml = """
            <disk device="disk" snapshot="no" type="block">
                <source dev="/path/to/volume"/>
                <target bus="virtio" dev="vda"/>
                <serial>54-a672-23e5b495a9ea</serial>
                <driver cache="none" discard="unmap" error_policy="stop"
                        io="native" name="qemu" type="raw"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=True)

    def test_disk_with_discard_off(self):
        conf = drive_config(
            serial='54-a672-23e5b495a9ea',
            discard=False,
        )
        xml = """
            <disk device="disk" snapshot="no" type="block">
                <source dev="/path/to/volume"/>
                <target bus="virtio" dev="vda"/>
                <serial>54-a672-23e5b495a9ea</serial>
                <driver cache="none" error_policy="stop"
                        io="native" name="qemu" type="raw"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=True)

    def test_disk_file(self):
        conf = drive_config(
            serial='54-a672-23e5b495a9ea',
        )
        xml = """
            <disk device="disk" snapshot="no" type="file">
                <source file="/path/to/volume"/>
                <target bus="virtio" dev="vda"/>
                <serial>54-a672-23e5b495a9ea</serial>
                <driver cache="none" error_policy="stop"
                        io="threads" name="qemu" type="raw"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=False)

    def test_lun(self):
        conf = drive_config(
            device='lun',
            iface='scsi',
            path='/dev/mapper/lun1',
            serial='54-a672-23e5b495a9ea',
            sgio='unfiltered',
        )
        xml = """
            <disk device="lun" sgio="unfiltered" snapshot="no" type="block">
                <source dev="/dev/mapper/lun1"/>
                <target bus="scsi" dev="sda"/>
                <driver cache="none" error_policy="stop"
                        io="native" name="qemu" type="raw"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=True)

    def test_network(self):
        conf = drive_config(
            diskType=DISK_TYPE.NETWORK,
            hosts=[
                dict(name='1.2.3.41', port='6789', transport='tcp'),
                dict(name='1.2.3.42', port='6789', transport='tcp'),
            ],
            path='poolname/volumename',
            protocol='rbd',
            serial='54-a672-23e5b495a9ea',
        )
        xml = """
            <disk device="disk" snapshot="no" type="network">
                <source name="poolname/volumename" protocol="rbd">
                    <host name="1.2.3.41" port="6789" transport="tcp"/>
                    <host name="1.2.3.42" port="6789" transport="tcp"/>
                </source>
                <target bus="virtio" dev="vda"/>
                <serial>54-a672-23e5b495a9ea</serial>
                <driver cache="none" error_policy="stop"
                        io="threads" name="qemu" type="raw"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=None)

    def test_network_with_auth(self):
        conf = drive_config(
            auth={"type": "ceph", "uuid": "abcdef", "username": "cinder"},
            diskType=DISK_TYPE.NETWORK,
            hosts=[
                dict(name='1.2.3.41', port='6789', transport='tcp'),
                dict(name='1.2.3.42', port='6789', transport='tcp'),
            ],
            path='poolname/volumename',
            protocol='rbd',
            serial='54-a672-23e5b495a9ea',
        )
        xml = """
            <disk device="disk" snapshot="no" type="network">
                <source name="poolname/volumename" protocol="rbd">
                    <host name="1.2.3.41" port="6789" transport="tcp"/>
                    <host name="1.2.3.42" port="6789" transport="tcp"/>
                </source>
                <auth username="cinder">
                    <secret type="ceph" uuid="abcdef"/>
                </auth>
                <target bus="virtio" dev="vda"/>
                <serial>54-a672-23e5b495a9ea</serial>
                <driver cache="none" error_policy="stop"
                        io="threads" name="qemu" type="raw"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=None)

    def test_cdrom_without_serial(self):
        conf = drive_config(
            device='cdrom',
            iface='ide',
            index='2',
            path='/path/to/fedora.iso',
            readonly='True',
        )
        xml = """
            <disk device="cdrom" snapshot="no" type="file">
                <source file="/path/to/fedora.iso" startupPolicy="optional"/>
                <target bus="ide" dev="hdc"/>
                <readonly/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=False)

    def test_disk_without_serial(self):
        conf = drive_config()
        xml = """
            <disk device="disk" snapshot="no" type="file">
                <source file="/path/to/volume"/>
                <target bus="virtio" dev="vda"/>
                <driver cache="none" error_policy="stop"
                        io="threads" name="qemu" type="raw"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=False)

    def check(self, vm_conf, device_conf, xml, is_block_device=False):
        drive = Drive(vm_conf, self.log, **device_conf)
        # Patch to skip the block device checking.
        if is_block_device is not None:
            drive._blockDev = is_block_device
        self.assertXMLEqual(vmxml.format_xml(drive.getXML()), xml)


class DriveReplicaXML(XMLTestCase):

    # Replica XML should match Drive XML using same diskType, cache and
    # propagateErrors settings.  Only the source and driver elements are used
    # by libvirt.
    # https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainBlockCopy

    def test_block_to_block(self):
        conf = drive_config(
            format='cow',
            diskReplicate=replica(DISK_TYPE.BLOCK),
        )
        # source: type=block
        # driver: io=native
        xml = """
            <disk device="disk" snapshot="no" type="block">
                <source dev="/path/to/replica"/>
                <driver cache="none" error_policy="stop"
                        io="native" name="qemu" type="qcow2"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=True)

    def test_block_to_file(self):
        conf = drive_config(
            format='cow',
            diskReplicate=replica(DISK_TYPE.FILE),
        )
        # source: type=file
        # driver: io=threads
        xml = """
            <disk device="disk" snapshot="no" type="file">
                <source file="/path/to/replica"/>
                <driver cache="none" error_policy="stop"
                        io="threads" name="qemu" type="qcow2"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=True)

    def test_file_to_file(self):
        conf = drive_config(
            format='cow',
            diskReplicate=replica(DISK_TYPE.FILE),
        )
        # source: type=file
        # driver: io=threads
        xml = """
            <disk device="disk" snapshot="no" type="file">
                <source file="/path/to/replica"/>
                <driver cache="none" error_policy="stop"
                        io="threads" name="qemu" type="qcow2"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=False)

    def test_file_to_block(self):
        conf = drive_config(
            format='cow',
            diskReplicate=replica(DISK_TYPE.BLOCK),
        )
        # source: type=block
        # driver: io=native
        xml = """
            <disk device="disk" snapshot="no" type="block">
                <source dev="/path/to/replica"/>
                <driver cache="none" error_policy="stop"
                        io="native" name="qemu" type="qcow2"/>
            </disk>
            """
        self.check({}, conf, xml, is_block_device=False)

    def check(self, vm_conf, device_conf, xml, is_block_device=False):
        drive = Drive(vm_conf, self.log, **device_conf)
        # Patch to skip the block device checking.
        drive._blockDev = is_block_device
        self.assertXMLEqual(vmxml.format_xml(drive.getReplicaXML()), xml)


@expandPermutations
class DriveValidation(VdsmTestCase):

    @permutations([["disk"], ["cdrom"], ["floppy"]])
    def test_sgio_without_lun(self, device):
        self.check(device=device, sgio='unfiltered')

    def test_cow_with_lun(self):
        self.check(device='lun', format='cow')

    def test_network_disk_no_hosts(self):
        self.check(diskType=DISK_TYPE.NETWORK, protocol='rbd')

    def test_network_disk_no_protocol(self):
        self.check(diskType=DISK_TYPE.NETWORK, hosts=[{}])

    def check(self, **kw):
        conf = drive_config(**kw)
        drive = Drive({}, self.log, **conf)
        self.assertRaises(ValueError, drive.getXML)


@expandPermutations
class DriveExSharedStatusTests(VdsmTestCase):

    def test_default_not_shared(self):
        self.check(None, 'none')

    @permutations([['exclusive'], ['shared'], ['none'], ['transient']])
    def test_supported(self, shared):
        self.check(shared, shared)

    def test_unsupported(self):
        self.assertRaises(ValueError, self.check, "UNKNOWN-VALUE", None)

    @permutations([[True], ['True'], ['true']])
    def test_bc_shared(self, shared):
        self.check(shared, 'shared')

    @permutations([[False], ['False'], ['false']])
    def test_bc_not_shared(self, shared):
        self.check(shared, 'none')

    def check(self, shared, expected):
        conf = drive_config()
        if shared:
            conf['shared'] = shared
        drive = Drive({}, self.log, **conf)
        self.assertEqual(drive.extSharedState, expected)


class DriveDiskTypeTests(VdsmTestCase):

    def test_cdrom(self):
        conf = drive_config(device='cdrom')
        drive = Drive({}, self.log, **conf)
        self.assertFalse(drive.networkDev)
        self.assertFalse(drive.blockDev)

    def test_floppy(self):
        conf = drive_config(device='floppy')
        drive = Drive({}, self.log, **conf)
        self.assertFalse(drive.networkDev)
        self.assertFalse(drive.blockDev)

    def test_network_disk(self):
        conf = drive_config(diskType=DISK_TYPE.NETWORK)
        drive = Drive({}, self.log, **conf)
        self.assertTrue(drive.networkDev)
        self.assertFalse(drive.blockDev)

    @MonkeyPatch(utils, 'isBlockDevice', lambda path: True)
    def test_block_disk(self):
        conf = drive_config(device='disk')
        drive = Drive({}, self.log, **conf)
        self.assertFalse(drive.networkDev)
        self.assertTrue(drive.blockDev)

    @MonkeyPatch(utils, 'isBlockDevice', lambda path: False)
    def test_file_disk(self):
        conf = drive_config(device='disk')
        drive = Drive({}, self.log, **conf)
        self.assertFalse(drive.networkDev)
        self.assertFalse(drive.blockDev)

    @MonkeyPatch(utils, 'isBlockDevice', lambda path: False)
    def test_migrate_from_file_to_block(self):
        conf = drive_config(path='/filedomain/volume')
        drive = Drive({}, self.log, **conf)
        self.assertFalse(drive.blockDev)
        # Migrate drive to block domain...
        utils.isBlockDevice = lambda path: True
        drive.path = "/blockdomain/volume"
        self.assertTrue(drive.blockDev)

    @MonkeyPatch(utils, 'isBlockDevice', lambda path: True)
    def test_migrate_from_block_to_file(self):
        conf = drive_config(path='/blockdomain/volume')
        drive = Drive({}, self.log, **conf)
        self.assertTrue(drive.blockDev)
        # Migrate drive to file domain...
        utils.isBlockDevice = lambda path: False
        drive.path = "/filedomain/volume"
        self.assertFalse(drive.blockDev)

    @MonkeyPatch(utils, 'isBlockDevice', lambda path: True)
    def test_migrate_from_block_to_network(self):
        conf = drive_config(path='/blockdomain/volume')
        drive = Drive({}, self.log, **conf)
        self.assertTrue(drive.blockDev)
        # Migrate drive to network disk...
        drive.path = "pool/volume"
        drive.diskType = DISK_TYPE.NETWORK
        self.assertFalse(drive.blockDev)

    @MonkeyPatch(utils, 'isBlockDevice', lambda path: True)
    def test_migrate_network_to_block(self):
        conf = drive_config(diskType=DISK_TYPE.NETWORK, path='pool/volume')
        drive = Drive({}, self.log, **conf)
        self.assertTrue(drive.networkDev)
        # Migrate drive to block domain...
        drive.path = '/blockdomain/volume'
        drive.diskType = None
        self.assertTrue(drive.blockDev)


@expandPermutations
class ChunkedTests(VdsmTestCase):

    @permutations([
        # device, blockDev, format, chunked
        ('cdrom', True, 'raw', False),
        ('cdrom', False, 'raw', False),
        ('floppy', False, 'raw', False),
        ('disk', False, 'raw', False),
        ('disk', True, 'raw', False),
        ('lun', True, 'raw', False),
        ('disk', True, 'cow', True),
    ])
    def test_drive(self, device, blockDev, format, chunked):
        conf = drive_config(device=device, format=format)
        drive = Drive({}, self.log, **conf)
        drive._blockDev = blockDev
        self.assertEqual(drive.chunked, chunked)

    @permutations([
        # replica diskType, replica format
        (DISK_TYPE.BLOCK, 'raw'),
        (DISK_TYPE.BLOCK, 'cow'),
    ])
    def test_replica(self, diskType, format):
        conf = drive_config(diskReplicate=replica(diskType, format=format))
        drive = Drive({}, self.log, **conf)
        drive._blockDev = False
        self.assertEqual(drive.chunked, False)


@expandPermutations
class ReplicaChunkedTests(VdsmTestCase):

    @permutations([
        # replica diskType, replica format, chunked
        (DISK_TYPE.FILE, 'raw', False),
        (DISK_TYPE.FILE, 'cow', False),
        (DISK_TYPE.BLOCK, 'raw', False),
        (DISK_TYPE.BLOCK, 'cow', True),
    ])
    def test_replica(self, diskType, format, chunked):
        conf = drive_config(diskReplicate=replica(diskType, format=format))
        drive = Drive({}, self.log, **conf)
        self.assertEqual(drive.replicaChunked, chunked)

    def test_no_replica(self):
        conf = drive_config()
        drive = Drive({}, self.log, **conf)
        self.assertEqual(drive.replicaChunked, False)


@expandPermutations
class DriveVolumeSizeTests(VdsmTestCase):

    CAPACITY = 8192 * constants.MEGAB

    @permutations([[1024 * constants.MEGAB], [2048 * constants.MEGAB]])
    def test_next_size(self, cursize):
        conf = drive_config(format='cow')
        drive = Drive({}, self.log, **conf)
        self.assertEqual(drive.getNextVolumeSize(cursize, self.CAPACITY),
                         cursize + drive.volExtensionChunk)

    @permutations([[CAPACITY - 1], [CAPACITY], [CAPACITY + 1]])
    def test_next_size_limit(self, cursize):
        conf = drive_config(format='cow')
        drive = Drive({}, self.log, **conf)
        self.assertEqual(drive.getNextVolumeSize(cursize, self.CAPACITY),
                         drive.getMaxVolumeSize(self.CAPACITY))

    def test_max_size(self):
        conf = drive_config(format='cow')
        drive = Drive({}, self.log, **conf)
        size = utils.round(self.CAPACITY * drive.VOLWM_COW_OVERHEAD,
                           constants.MEGAB)
        self.assertEqual(drive.getMaxVolumeSize(self.CAPACITY), size)


@expandPermutations
class TestDriveLeases(XMLTestCase):
    """
    To have leases, drive must have a non-empty volumeChain,
    shared="exclusive", or shared="false" and irs:use_volume_leases=True.

    Any other setting results in no leases.
    """

    # Drive without leases

    @MonkeyPatch(storage, 'config', make_config([
        ("irs", "use_volume_leases", "false")
    ]))
    @permutations([
        ["true"],
        ["True"],
        ["TRUE"],
        ["false"],
        ["False"],
        ["FALSE"],
        [DRIVE_SHARED_TYPE.NONE],
        [DRIVE_SHARED_TYPE.EXCLUSIVE],
        [DRIVE_SHARED_TYPE.SHARED],
        [DRIVE_SHARED_TYPE.TRANSIENT],
    ])
    def test_shared_no_volume_leases_no_chain(self, shared):
        conf = drive_config(shared=shared, volumeChain=[])
        self.check_no_leases(conf)

    @MonkeyPatch(storage, 'config', make_config([
        ("irs", "use_volume_leases", "true")
    ]))
    @permutations([
        ["true"],
        ["True"],
        ["TRUE"],
        ["false"],
        ["False"],
        ["FALSE"],
        [DRIVE_SHARED_TYPE.NONE],
        [DRIVE_SHARED_TYPE.EXCLUSIVE],
        [DRIVE_SHARED_TYPE.SHARED],
        [DRIVE_SHARED_TYPE.TRANSIENT],
    ])
    def test_shared_use_volume_leases_no_chain(self, shared):
        conf = drive_config(shared=shared, volumeChain=[])
        self.check_no_leases(conf)

    # Drive with leases

    @MonkeyPatch(storage, 'config', make_config([
        ("irs", "use_volume_leases", "true")
    ]))
    @permutations([
        ["false"],
        [DRIVE_SHARED_TYPE.EXCLUSIVE],
    ])
    def test_use_volume_leases(self, shared):
        conf = drive_config(shared=shared, volumeChain=make_volume_chain())
        self.check_leases(conf)

    @MonkeyPatch(storage, 'config', make_config([
        ("irs", "use_volume_leases", "false")
    ]))
    @permutations([
        [DRIVE_SHARED_TYPE.EXCLUSIVE],
    ])
    def test_no_volume_leases(self, shared):
        conf = drive_config(shared=shared, volumeChain=make_volume_chain())
        self.check_leases(conf)

    # Helpers

    def check_no_leases(self, conf):
        drive = Drive({}, self.log, **conf)
        leases = list(drive.getLeasesXML())
        self.assertEqual([], leases)

    def check_leases(self, conf):
        drive = Drive({}, self.log, **conf)
        leases = list(drive.getLeasesXML())
        self.assertEqual(1, len(leases))
        xml = """
        <lease>
            <key>vol_id</key>
            <lockspace>dom_id</lockspace>
            <target offset="0" path="path" />
        </lease>
        """
        self.assertXMLEqual(vmxml.format_xml(leases[0]), xml)


@expandPermutations
class TestDriveNaming(VdsmTestCase):

    @permutations([
        ['ide', -1, 'hda'],
        ['ide', 0, 'hda'],
        ['ide', 1, 'hdb'],
        ['ide', 2, 'hdc'],
        ['ide', 25, 'hdz'],
        ['ide', 26, 'hdba'],
        ['ide', 27, 'hdbb'],

        ['scsi', -1, 'sda'],
        ['scsi', 0, 'sda'],
        ['scsi', 1, 'sdb'],
        ['scsi', 2, 'sdc'],
        ['scsi', 25, 'sdz'],
        ['scsi', 26, 'sdba'],
        ['scsi', 27, 'sdbb'],

        ['virtio', -1, 'vda'],
        ['virtio', 0, 'vda'],
        ['virtio', 1, 'vdb'],
        ['virtio', 2, 'vdc'],
        ['virtio', 25, 'vdz'],
        ['virtio', 26, 'vdba'],
        ['virtio', 27, 'vdbb'],

        ['fdc', -1, 'fda'],
        ['fdc', 0, 'fda'],
        ['fdc', 1, 'fdb'],
        ['fdc', 2, 'fdc'],
        ['fdc', 25, 'fdz'],
        ['fdc', 26, 'fdba'],
        ['fdc', 27, 'fdbb'],

        ['sata', -1, 'sda'],
        ['sata', 0, 'sda'],
        ['sata', 1, 'sdb'],
        ['sata', 2, 'sdc'],
        ['sata', 25, 'sdz'],
        ['sata', 26, 'sdba'],
        ['sata', 27, 'sdbb'],
    ])
    def test_ide_drive(self, interface, index, expected_name):
        conf = drive_config(
            device='disk',
            iface=interface,
            index=index,
        )

        drive = Drive({}, self.log, **conf)
        self.assertEqual(drive.name, expected_name)


class TestVolumePath(VdsmTestCase):
    def setUp(self):
        volume_chain = [{"path": "/top",
                         "volumeID": "00000000-0000-0000-0000-000000000000"},
                        {"path": "/base",
                         "volumeID": "11111111-1111-1111-1111-111111111111"}]
        self.conf = drive_config(volumeChain=volume_chain)

    def test_correct_extraction(self):
        drive = Drive({}, self.log, **self.conf)
        actual = drive.volume_path("11111111-1111-1111-1111-111111111111")
        self.assertEqual(actual, "/base")

    def test_base_not_found(self):
        drive = Drive({}, self.log, **self.conf)
        with self.assertRaises(storage.VolumeNotFound):
            drive.volume_path("F1111111-1111-1111-1111-111111111111")


class TestVolumeChain(VdsmTestCase):
    def setUp(self):
        volume_chain = [{'path': '/foo/bar',
                         'volumeID': '11111111-1111-1111-1111-111111111111'},
                        {'path': '/foo/zap',
                         'volumeID': '22222222-2222-2222-2222-222222222222'}]
        conf = drive_config(volumeChain=volume_chain)
        self.drive = Drive({}, self.log, **conf)
        self.drive._blockDev = True

    def test_parse_volume_chain(self):
        disk_xml = etree.fromstring("""
<disk type='block' device='disk' snapshot='no'>
    <driver name='qemu' type='qcow2' cache='none'
        error_policy='stop' io='native'/>
    <source dev='/foo/bar'/>
    <backingStore type='block' index='1'>
        <format type='raw'/>
        <source dev='/foo/zap'/>
        <backingStore/>
    </backingStore>
    <target dev='vda' bus='virtio'/>
    <serial>10ff6010-4b56-4d78-9814-b9559bccb5a0</serial>
    <boot order='1'/>
    <alias name='virtio-disk0'/>
    <address type='pci' domain='0x0000'
        bus='0x00' slot='0x05' function='0x0'/>
</disk>""")

        info = self.drive.parse_volume_chain(disk_xml)
        expected = [
            storage.VolumeChainEntry(
                path='/foo/zap',
                allocation=None,
                uuid='22222222-2222-2222-2222-222222222222'),
            storage.VolumeChainEntry(
                path='/foo/bar',
                allocation=None,
                uuid='11111111-1111-1111-1111-111111111111')
        ]
        self.assertEqual(info, expected)


def make_volume_chain(path="path", offset=0, vol_id="vol_id", dom_id="dom_id"):
    return [{"leasePath": path,
             "leaseOffset": offset,
             "volumeID": vol_id,
             "domainID": dom_id}]


def drive_config(**kw):
    """ Return drive configuration updated from **kw """
    conf = {
        'device': 'disk',
        'format': 'raw',
        'iface': 'virtio',
        'index': '0',
        'path': '/path/to/volume',
        'propagateErrors': 'off',
        'readonly': 'False',
        'shared': 'none',
        'type': 'disk',
    }
    conf.update(kw)
    return conf


def replica(diskType, format="cow"):
    return {
        "cache": "none",
        "device": "disk",
        "diskType": diskType,
        "format": format,
        "path": "/path/to/replica",
        "propagateErrors": "off",
    }
