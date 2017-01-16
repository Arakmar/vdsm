# Copyright 2014 Red Hat, Inc.
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

from contextlib import contextmanager
from io import StringIO
import subprocess
import tarfile
import zipfile

import libvirt
import os

from testlib import namedTemporaryDir, permutations, expandPermutations
from v2v_testlib import VM_SPECS, MockVirDomain, MockVirConnect
from v2v_testlib import _mac_from_uuid, BLOCK_DEV_PATH
from vdsm import v2v
from vdsm import libvirtconnection
from vdsm.password import ProtectedPassword
from vdsm.commands import execCmd
from vdsm.utils import CommandPath


from testlib import VdsmTestCase as TestCaseBase, recorded
from monkeypatch import MonkeyPatch, MonkeyPatchScope

import vmfakelib as fake


FAKE_VIRT_V2V = CommandPath('fake-virt-v2v',
                            os.path.abspath('fake-virt-v2v'))
FAKE_SSH_ADD = CommandPath('fake-ssh-add',
                           os.path.abspath('fake-ssh-add'))
FAKE_SSH_AGENT = CommandPath('fake-ssh-agent',
                             os.path.abspath('fake-ssh-agent'))


def legacylistAllDomains():
    raise fake.Error(libvirt.VIR_ERR_NO_SUPPORT,
                     'Method not supported')


def legacylistAllDomainsWrongRaise():
    raise fake.Error(libvirt.VIR_ERR_NO_DOMAIN,
                     'Domain not exists')


def lookupByNameFailure(name):
    raise fake.Error(libvirt.VIR_ERR_NO_DOMAIN,
                     'Domain not exists')


def lookupByIDFailure(id):
    raise fake.Error(libvirt.VIR_ERR_NO_DOMAIN,
                     'Domain not exists')


class FakeIRS(object):
    @recorded
    def prepareImage(self, domainId, poolId, imageId, volumeId):
        return {'status': {'code': 0},
                'path': os.path.join('/rhev/data-center', poolId, domainId,
                                     'images', imageId, volumeId)}

    @recorded
    def teardownImage(self, domainId, poolId, imageId):
        return 0


def read_ovf(ovf_path):
    return """<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_Re\
sourceAllocationSettingData">
  <References>
    <File ovf:href="First-disk1.vmdk" ovf:id="file1" ovf:size="349405696"/>
    <File ovf:href="First-disk2.vmdk" ovf:id="file2" ovf:size="349405696"/>
  </References>
  <DiskSection>
    <Disk ovf:capacity="32" ovf:fileRef="file1"/>
    <Disk ovf:capacity="32" ovf:populatedSize="698811392" ovf:fileRef="file2"/>
  </DiskSection>
  <VirtualSystem ovf:id="First">
    <Name>First</Name>
    <VirtualHardwareSection>
      <Item>
        <rasd:ResourceType>4</rasd:ResourceType>
        <rasd:VirtualQuantity>2048</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:ResourceType>3</rasd:ResourceType>
        <rasd:VirtualQuantity>1</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:Connection>VM Network</rasd:Connection>
        <rasd:ElementName>Ethernet 1</rasd:ElementName>
        <rasd:ResourceSubType>E1000</rasd:ResourceSubType>
        <rasd:ResourceType>10</rasd:ResourceType>
      </Item>
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>"""


@contextmanager
def temporary_ovf_dir():
    with namedTemporaryDir() as base:
        ovfpath = os.path.join(base, 'testvm.ovf')
        ovapath = os.path.join(base, 'testvm.ova')
        ovf = read_ovf('test')

        with open(ovfpath, 'w') as ovffile:
            ovffile.write(ovf)
        yield ovapath


@expandPermutations
class v2vTests(TestCaseBase):
    def setUp(self):
        '''
        We are testing the output of fake-virt-v2v with vminfo input
        against pre-saved output;
        Do not change this parameters without modifying
        fake-virt-v2v.output.
        '''
        self.vm_name = 'TEST'
        self.job_id = '00000000-0000-0000-0000-000000000005'
        self.pool_id = '00000000-0000-0000-0000-000000000006'
        self.domain_id = '00000000-0000-0000-0000-000000000007'
        self.image_id_a = '00000000-0000-0000-0000-000000000001'
        self.volume_id_a = '00000000-0000-0000-0000-000000000002'
        self.image_id_b = '00000000-0000-0000-0000-000000000003'
        self.volume_id_b = '00000000-0000-0000-0000-000000000004'
        self.vpx_url = 'vpx://adminr%40vsphere@0.0.0.0/ovirt/' \
                       '0.0.0.0?no_verify=1'
        self.xen_url = 'xen+ssh://user@host.com'

        self.vminfo = {'vmName': self.vm_name,
                       'poolID': self.pool_id,
                       'domainID': self.domain_id,
                       'disks': [{'imageID': self.image_id_a,
                                  'volumeID': self.volume_id_a},
                                 {'imageID': self.image_id_b,
                                  'volumeID': self.volume_id_b}]}

        self._vms = [MockVirDomain(*spec) for spec in VM_SPECS]

    def tearDown(self):
        v2v._jobs.clear()

    def testGetExternalVMs(self):
        def _connect(uri, username, passwd):
            return MockVirConnect(vms=self._vms)

        with MonkeyPatchScope([(libvirtconnection, 'open_connection',
                                _connect)]):
            vms = v2v.get_external_vms('esx://mydomain', 'user',
                                       ProtectedPassword('password'),
                                       None)['vmList']

        self.assertEqual(len(vms), len(VM_SPECS))

        for vm, spec in zip(vms, VM_SPECS):
            self._assertVmMatchesSpec(vm, spec)
            self._assertVmDisksMatchSpec(vm, spec)

    def testGetExternalVMsList(self):
        def _connect(uri, username, passwd):
            return MockVirConnect(vms=self._vms)

        vmIDs = [1, 3]
        names = [vm.name for vm in VM_SPECS if vm.id in vmIDs]
        # Add a non-existent name to check that nothing bad happens.
        names.append('Some nonexistent name')

        with MonkeyPatchScope([(libvirtconnection, 'open_connection',
                                _connect)]):
            vms = v2v.get_external_vms('esx://mydomain', 'user',
                                       ProtectedPassword('password'),
                                       names)['vmList']

        self.assertEqual(len(vms), len(vmIDs))

        for vm, vmID in zip(vms, vmIDs):
            spec = VM_SPECS[vmID]
            self._assertVmMatchesSpec(vm, spec)
            self._assertVmDisksMatchSpec(vm, spec)

    def testGetExternalVMNames(self):
        def _connect(uri, username, passwd):
            return MockVirConnect(vms=self._vms)

        with MonkeyPatchScope([(libvirtconnection, 'open_connection',
                                _connect)]):
            vms = v2v.get_external_vm_names(
                'esx://mydomain', 'user',
                ProtectedPassword('password'))['vmNames']

        self.assertEqual(
            sorted(vms),
            sorted(spec.name for spec in VM_SPECS))

    def testGetExternalVMsWithXMLDescFailure(self):
        specs = list(VM_SPECS)

        def internal_error(flags=0):
            raise fake.Error(libvirt.VIR_ERR_INTERNAL_ERROR)

        fake_vms = [MockVirDomain(*spec) for spec in specs]
        # Cause vm 1 to fail, so it would not appear in results
        fake_vms[1].XMLDesc = internal_error
        del specs[1]

        def _connect(uri, username, passwd):
            return MockVirConnect(vms=fake_vms)

        with MonkeyPatchScope([(libvirtconnection, 'open_connection',
                                _connect)]):
            vms = v2v.get_external_vms('esx://mydomain', 'user',
                                       ProtectedPassword('password'),
                                       None)['vmList']

        self.assertEqual(len(vms), len(specs))

        for vm, spec in zip(vms, specs):
            self._assertVmMatchesSpec(vm, spec)
            self._assertVmDisksMatchSpec(vm, spec)

    def testLegacyGetExternalVMs(self):
        def _connect(uri, username, passwd):
            mock = MockVirConnect(vms=self._vms)
            mock.listAllDomains = legacylistAllDomains
            return mock

        with MonkeyPatchScope([(libvirtconnection, 'open_connection',
                                _connect)]):
            vms = v2v.get_external_vms('esx://mydomain', 'user',
                                       ProtectedPassword('password'),
                                       None)['vmList']
            self.assertEqual(len(vms), len(self._vms))

    def testLegacyGetExternalVMsFailure(self):
        def _connect(uri, username, passwd):
            mock = MockVirConnect(vms=self._vms)
            mock.listAllDomains = legacylistAllDomainsWrongRaise
            return mock

        with MonkeyPatchScope([(libvirtconnection, 'open_connection',
                                _connect)]):
            self.assertRaises(libvirt.libvirtError,
                              v2v.get_external_vms,
                              'esx://mydomain', 'user',
                              ProtectedPassword('password'),
                              None)

    @permutations([
        # (methodname, fakemethod, active)
        ['lookupByName', lookupByNameFailure, True],
        ['lookupByID', lookupByIDFailure, False]
    ])
    def testLookupFailure(self, methodname, fakemethod, active):
        def _connect(uri, username, passwd):
            mock = MockVirConnect(vms=self._vms)
            mock.listAllDomains = legacylistAllDomains
            setattr(mock, methodname, fakemethod)
            return mock

        with MonkeyPatchScope([(libvirtconnection, 'open_connection',
                                _connect)]):
            vms = v2v.get_external_vms('esx://mydomain', 'user',
                                       ProtectedPassword('password'),
                                       None)['vmList']
            self.assertEqual(
                sorted(vm['vmName'] for vm in vms),
                sorted(spec.name for spec in VM_SPECS
                       if spec.active == active)
            )

    def testOutputParser(self):
        output = (u'[   0.0] Opening the source -i libvirt ://roo...\n'
                  u'[   1.0] Creating an overlay to protect the f...\n'
                  u'[  88.0] Copying disk 1/2 to /tmp/v2v/0000000...\n'
                  u'    (0/100%)\r'
                  u'    (50/100%)\r'
                  u'    (100/100%)\r'
                  u'[ 180.0] Copying disk 2/2 to /tmp/v2v/100000-...\n'
                  u'    (0/100%)\r'
                  u'    (50/100%)\r'
                  u'    (100/100%)\r'
                  u'[ 256.0] Creating output metadata'
                  u'[ 256.0] Finishing off')

        parser = v2v.OutputParser()
        events = list(parser.parse(StringIO(output)))
        self.assertEqual(events, [
            (v2v.ImportProgress(1, 2, 'Copying disk 1/2')),
            (v2v.DiskProgress(0)),
            (v2v.DiskProgress(50)),
            (v2v.DiskProgress(100)),
            (v2v.ImportProgress(2, 2, 'Copying disk 2/2')),
            (v2v.DiskProgress(0)),
            (v2v.DiskProgress(50)),
            (v2v.DiskProgress(100))])

    def testGetExternalVMsWithoutDisksInfo(self):
        def internal_error(name):
            raise fake.Error(libvirt.VIR_ERR_INTERNAL_ERROR)

        # we need a sequence of just one vm
        mock = MockVirConnect(vms=self._vms[:1])
        mock.storageVolLookupByPath = internal_error

        def _connect(uri, username, passwd):
            return mock

        with MonkeyPatchScope([(libvirtconnection, 'open_connection',
                                _connect)]):
            vms = v2v.get_external_vms('esx://mydomain', 'user',
                                       ProtectedPassword('password'),
                                       None)['vmList']
        self.assertEquals(len(vms), 0)

    def _assertVmDisksMatchSpec(self, vm, spec):
        disk = vm['disks'][0]
        self.assertEquals(disk['dev'], 'sda')
        self.assertEquals(disk['alias'],
                          '[datastore1] RHEL/RHEL_%s.vmdk' % spec.name)
        self.assertIn('capacity', disk)
        self.assertIn('allocation', disk)

    def _assertVmMatchesSpec(self, vm, spec):
        self.assertEquals(vm['vmId'], spec.uuid)
        self.assertEquals(vm['memSize'], 2048)
        self.assertEquals(vm['smp'], 1)
        self.assertEquals(len(vm['disks']), 1)
        self.assertEquals(len(vm['networks']), 1)
        self.assertEquals(vm['has_snapshots'], spec.has_snapshots)

        network = vm['networks'][0]
        self.assertEquals(network['type'], 'bridge')
        self.assertEquals(network['macAddr'], _mac_from_uuid(spec.uuid))
        self.assertEquals(network['bridge'], 'VM Network')

    def testSuccessfulVMWareImport(self):
        self._commonConvertExternalVM(self.vpx_url)

    @MonkeyPatch(v2v, '_SSH_ADD', FAKE_SSH_ADD)
    @MonkeyPatch(v2v, '_SSH_AGENT', FAKE_SSH_AGENT)
    def testSuccessfulXenImport(self):
        self._commonConvertExternalVM(self.xen_url)

    def testBlockDevice(self):
        def _connect(uri, username, passwd):
            for vm in self._vms:
                vm.setDiskType('block')
            conn = MockVirConnect(vms=self._vms)
            return conn

        with MonkeyPatchScope([(libvirtconnection, 'open_connection',
                                _connect)]):
            vms = v2v.get_external_vms(self.xen_url, 'user',
                                       ProtectedPassword('password'),
                                       None)['vmList']

        self.assertEqual(len(vms), len(VM_SPECS))
        self.assertEqual(BLOCK_DEV_PATH, vms[0]['disks'][0]['alias'])

    def testXenBlockDevice(self):
        def _connect(uri, username, passwd):
            self._vms[0].setDiskType('block')
            conn = MockVirConnect(vms=self._vms)
            conn.setType('Xen')
            return conn

        with MonkeyPatchScope([(libvirtconnection, 'open_connection',
                                _connect)]):
            vms = v2v.get_external_vms(self.xen_url, 'user',
                                       ProtectedPassword('password'),
                                       None)['vmList']

        self.assertEqual(len(vms), len(VM_SPECS) - 1)
        self.assertTrue(self._vms[0] not in vms)

    @MonkeyPatch(v2v, '_VIRT_V2V', FAKE_VIRT_V2V)
    @MonkeyPatch(v2v, '_LOG_DIR', None)
    def testSuccessfulImportOVA(self):
        with temporary_ovf_dir() as ovapath, \
                namedTemporaryDir() as v2v._LOG_DIR:
            v2v.convert_ova(ovapath, self.vminfo, self.job_id, FakeIRS())
            job = v2v._jobs[self.job_id]
            job.wait()

            self.assertEqual(job.status, v2v.STATUS.DONE)

    def testV2VOutput(self):
        cmd = [FAKE_VIRT_V2V.cmd,
               '-v',
               '-x',
               '-ic', self.vpx_url,
               '-o', 'vdsm',
               '-of', 'raw',
               '-oa', 'sparse',
               '--vdsm-image-uuid', self.image_id_a,
               '--vdsm-vol-uuid', self.volume_id_a,
               '--vdsm-image-uuid', self.image_id_b,
               '--vdsm-vol-uuid', self.volume_id_b,
               '--password-file', '/tmp/mypass',
               '--vdsm-vm-uuid', self.job_id,
               '--vdsm-ovf-output', '/usr/local/var/run/vdsm/v2v',
               '--machine-readable',
               '-os', '/rhev/data-center/%s/%s' % (self.pool_id,
                                                   self.domain_id),
               self.vm_name]

        rc, output, error = execCmd(cmd, raw=True)
        self.assertEqual(rc, 0)

        with open('fake-virt-v2v.out', 'r') as f:
            self.assertEqual(output, f.read())

        with open('fake-virt-v2v.err', 'r') as f:
            self.assertEqual(error, f.read())

    @MonkeyPatch(v2v, '_VIRT_V2V', FAKE_VIRT_V2V)
    @MonkeyPatch(v2v, '_V2V_DIR', None)
    @MonkeyPatch(v2v, '_LOG_DIR', None)
    def _commonConvertExternalVM(self, url):
        with namedTemporaryDir() as v2v._V2V_DIR, \
                namedTemporaryDir() as v2v._LOG_DIR:
            v2v.convert_external_vm(url,
                                    'root',
                                    ProtectedPassword('mypassword'),
                                    self.vminfo,
                                    self.job_id,
                                    FakeIRS())
            job = v2v._jobs[self.job_id]
            job.wait()

            self.assertEqual(job.status, v2v.STATUS.DONE)

    def testSimpleExecCmd(self):
        p = v2v._simple_exec_cmd(['cat'],
                                 stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE)
        msg = "test\ntest"
        p.stdin.write(msg)
        p.stdin.close()
        p.wait()
        out = p.stdout.read()
        self.assertEqual(out, msg)

        p = v2v._simple_exec_cmd(['/bin/sh', '-c', 'echo -en "%s" >&2' % msg],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        p.wait()
        out = p.stdout.read()
        self.assertEqual(out, msg)


@expandPermutations
class PipelineProcTests(TestCaseBase):

    PROC_WAIT_TIMEOUT = 30

    def testRun(self):
        msg = 'foo\nbar'
        p1 = v2v._simple_exec_cmd(['echo', '-n', msg],
                                  stdout=subprocess.PIPE)
        p2 = v2v._simple_exec_cmd(['cat'],
                                  stdin=p1.stdout,
                                  stdout=subprocess.PIPE)

        p = v2v.PipelineProc(p1, p2)
        self.assertEqual(p.pids, [p1.pid, p2.pid])

        ret = p.wait(self.PROC_WAIT_TIMEOUT)
        self.assertEqual(ret, True)

        out = p.stdout.read()
        self.assertEqual(out, msg)

    @permutations([
        # (cmd1, cmd2, returncode)
        ['false', 'true', 1],
        ['true', 'false', 1],
        ['true', 'true', 0],
    ])
    def testReturncode(self, cmd1, cmd2, returncode):
        p1 = v2v._simple_exec_cmd([cmd1],
                                  stdout=subprocess.PIPE)
        p2 = v2v._simple_exec_cmd([cmd2],
                                  stdin=p1.stdout,
                                  stdout=subprocess.PIPE)
        p = v2v.PipelineProc(p1, p2)
        p.wait(self.PROC_WAIT_TIMEOUT)
        self.assertEqual(p.returncode, returncode)

    @permutations([
        # (cmd1, cmd2, waitRet)
        [['sleep', '1'], ['sleep', '1'], True],
        [['sleep', '1'], ['sleep', '3'], False],
        [['sleep', '3'], ['sleep', '1'], False],
        [['sleep', '3'], ['sleep', '3'], False],
    ])
    def testWait(self, cmd1, cmd2, waitRet):
        p1 = v2v._simple_exec_cmd(cmd1,
                                  stdout=subprocess.PIPE)
        p2 = v2v._simple_exec_cmd(cmd2,
                                  stdin=p1.stdout,
                                  stdout=subprocess.PIPE)
        p = v2v.PipelineProc(p1, p2)
        ret = p.wait(2)
        p.kill()
        self.assertEqual(ret, waitRet)


class MockVirConnectTests(TestCaseBase):
    def setUp(self):
        self._vms = [MockVirDomain(*spec) for spec in VM_SPECS]
        self._mock = MockVirConnect(vms=self._vms)

    def test_list_all_domains(self):
        vms = self._mock.listAllDomains()
        self.assertEqual(len(vms), len(self._vms))

    def test_list_defined_domains(self):
        vms = self._mock.listDefinedDomains()
        self.assertEqual(
            sorted(vms),
            sorted(spec.name for spec in VM_SPECS if not spec.active))

    def test_list_domains_id(self):
        vms = self._mock.listDomainsID()
        self.assertEqual(len(vms), 2)

    def test_lookup_by_name(self):
        vm = self._mock.lookupByName('RHEL_0')
        self.assertEquals('RHEL_0', vm.name())

    def test_lookup_by_name_failed(self):
        self.assertRaises(libvirt.libvirtError, self._mock.lookupByName,
                          'fakename')

    def test_lookup_by_id(self):
        vm = self._mock.lookupByID(0)
        self.assertEquals(0, vm.ID())

    def test_lookup_by_id_failed(self):
        self.assertRaises(libvirt.libvirtError, self._mock.lookupByID, 99)


class TestGetOVAInfo(TestCaseBase):
    def test_directory(self):
        with self.temporary_ovf_dir() as (base, ovfpath, ovapath):
            vm = v2v.get_ova_info(base)
            self.check(vm['vmList'])

    def test_tar(self):
        with self.temporary_ovf_dir() as (base, ovfpath, ovapath):
            with tarfile.open(ovapath, 'w') as tar:
                tar.add(ovfpath, arcname='testvm.ovf')
            vm = v2v.get_ova_info(ovapath)
            self.check(vm['vmList'])

    def test_zip(self):
        with self.temporary_ovf_dir() as (base, ovfpath, ovapath):
            with zipfile.ZipFile(ovapath, 'w') as zip:
                zip.write(ovfpath)
            vm = v2v.get_ova_info(ovapath)
            self.check(vm['vmList'])

    @contextmanager
    def temporary_ovf_dir(self):
        with namedTemporaryDir() as base:
            ovfpath = os.path.join(base, 'testvm.ovf')
            ovapath = os.path.join(base, 'testvm.ova')
            ovf = read_ovf('test')

            with open(ovfpath, 'w') as ovffile:
                ovffile.write(ovf)
            yield base, ovfpath, ovapath

    def check(self, vm):
        self.assertEquals(vm['vmName'], 'First')
        self.assertEquals(vm['memSize'], 2048)
        self.assertEquals(vm['smp'], 1)

        disk = vm['disks'][0]
        self.assertEquals(disk['allocation'], '349405696')
        self.assertEquals(disk['capacity'], '34359738368')
        self.assertEquals(disk['type'], 'disk')
        self.assertEquals(disk['alias'], 'First-disk1.vmdk')

        network = vm['networks'][0]
        self.assertEquals(network['bridge'], 'VM Network')
        self.assertEquals(network['model'], 'E1000')
        self.assertEquals(network['type'], 'bridge')
        self.assertEquals(network['dev'], 'Ethernet 1')
