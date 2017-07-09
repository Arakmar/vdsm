#
# Copyright 2012 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301  USA
#
# Refer to the README and COPYING files for full details of the license
#
import __builtin__
import os
from datetime import datetime
from functools import partial
import io
import time

from vdsm import ipwrapper
from vdsm import netinfo
from vdsm.netinfo import OPERSTATE_UP
from vdsm.netlink import addr as nl_addr
from vdsm.utils import random_iface_name

from functional import dummy, veth
from ipwrapperTests import _fakeTypeDetection
from monkeypatch import MonkeyPatch, MonkeyPatchScope
from testlib import VdsmTestCase as TestCaseBase, namedTemporaryDir
from testValidation import ValidateRunningAsRoot, RequireBondingMod
from testValidation import brokentest

# speeds defined in ethtool
ETHTOOL_SPEEDS = set([10, 100, 1000, 2500, 10000])


class TestNetinfo(TestCaseBase):

    def testNetmaskConversions(self):
        path = os.path.join(os.path.dirname(__file__), "netmaskconversions")
        with open(path) as netmaskFile:
            for line in netmaskFile:
                if line.startswith('#'):
                    continue
                bitmask, address = [value.strip() for value in line.split()]
                self.assertEqual(netinfo.prefix2netmask(int(bitmask)),
                                 address)
        self.assertRaises(ValueError, netinfo.prefix2netmask, -1)
        self.assertRaises(ValueError, netinfo.prefix2netmask, 33)

    @MonkeyPatch(ipwrapper.Link, '_detectType',
                 partial(_fakeTypeDetection, ipwrapper.Link))
    def testSpeedInvalidNic(self):
        nicName = '0' * 20  # devices can't have so long names
        self.assertEqual(netinfo.nicSpeed(nicName), 0)

    @MonkeyPatch(ipwrapper.Link, '_detectType',
                 partial(_fakeTypeDetection, ipwrapper.Link))
    def testSpeedInRange(self):
        for d in netinfo.nics():
            s = netinfo.nicSpeed(d)
            self.assertFalse(s < 0)
            self.assertTrue(s in ETHTOOL_SPEEDS or s == 0)

    def testValidNicSpeed(self):
        values = ((0,           OPERSTATE_UP, 0),
                  (-10,         OPERSTATE_UP, 0),
                  (2 ** 16 - 1, OPERSTATE_UP, 0),
                  (2 ** 32 - 1, OPERSTATE_UP, 0),
                  (123,         OPERSTATE_UP, 123),
                  ('',          OPERSTATE_UP, 0),
                  ('',          'unknown',    0),
                  (123,         'unknown',    0))

        for passed, operstate, expected in values:
            with MonkeyPatchScope([(__builtin__, 'open',
                                    lambda x: io.BytesIO(str(passed))),
                                   (netinfo, 'operstate',
                                    lambda x: operstate)]):
                self.assertEqual(netinfo.nicSpeed('fake_nic'), expected)

    @MonkeyPatch(ipwrapper.Link, '_detectType',
                 partial(_fakeTypeDetection, ipwrapper.Link))
    @MonkeyPatch(netinfo, 'networks', lambda: {'fake': {'bridged': True}})
    @MonkeyPatch(netinfo, '_getBondingOptions', lambda x: {})
    def testGetNonExistantBridgeInfo(self):
        # Getting info of non existing bridge should not raise an exception,
        # just log a traceback. If it raises an exception the test will fail as
        # it should.
        netinfo.get()

    @MonkeyPatch(netinfo, 'getLinks', lambda: [])
    @MonkeyPatch(netinfo, 'networks', lambda: {})
    def testGetEmpty(self):
        result = {}
        result.update(netinfo.get())
        self.assertEqual(result['networks'], {})
        self.assertEqual(result['bridges'], {})
        self.assertEqual(result['nics'], {})
        self.assertEqual(result['bondings'], {})
        self.assertEqual(result['vlans'], {})

    def testIPv4toMapped(self):
        self.assertEqual('::ffff:127.0.0.1', netinfo.IPv4toMapped('127.0.0.1'))

    def testGetDeviceByIP(self):
        for addr in nl_addr.iter_addrs():
            # Link-local IPv6 addresses are generated from the MAC address,
            # which is shared between a nic and its bridge. Since We don't
            # support having the same IP address on two different NICs, and
            # link-local IPv6 addresses aren't interesting for 'getDeviceByIP'
            # then ignore them in the test
            if addr['scope'] != 'link':
                self.assertEqual(
                    addr['label'],
                    netinfo.getDeviceByIP(addr['address'].split('/')[0]))

    def _testNics(self):
        """Creates a test fixture so that nics() reports:
        physical nics: em, me, me0, me1, hid0 and hideous
        dummies: fake and fake0
        bonds: jbond (over me0 and me1)"""
        return [ipwrapper.Link(address='f0:de:f1:da:aa:e7', index=2,
                               linkType=ipwrapper.LinkType.NIC, mtu=1500,
                               name='em', qdisc='pfifo_fast', state='up'),
                ipwrapper.Link(address='ff:de:f1:da:aa:e7', index=3,
                               linkType=ipwrapper.LinkType.NIC, mtu=1500,
                               name='me', qdisc='pfifo_fast', state='up'),
                ipwrapper.Link(address='ff:de:fa:da:aa:e7', index=4,
                               linkType=ipwrapper.LinkType.NIC, mtu=1500,
                               name='hid0', qdisc='pfifo_fast', state='up'),
                ipwrapper.Link(address='ff:de:11:da:aa:e7', index=5,
                               linkType=ipwrapper.LinkType.NIC, mtu=1500,
                               name='hideous', qdisc='pfifo_fast', state='up'),
                ipwrapper.Link(address='66:de:f1:da:aa:e7', index=6,
                               linkType=ipwrapper.LinkType.NIC, mtu=1500,
                               name='me0', qdisc='pfifo_fast', state='up',
                               master='jbond'),
                ipwrapper.Link(address='66:de:f1:da:aa:e7', index=7,
                               linkType=ipwrapper.LinkType.NIC, mtu=1500,
                               name='me1', qdisc='pfifo_fast', state='up',
                               master='jbond'),
                ipwrapper.Link(address='ff:aa:f1:da:aa:e7', index=34,
                               linkType=ipwrapper.LinkType.DUMMY, mtu=1500,
                               name='fake0', qdisc='pfifo_fast', state='up'),
                ipwrapper.Link(address='ff:aa:f1:da:bb:e7', index=35,
                               linkType=ipwrapper.LinkType.DUMMY, mtu=1500,
                               name='fake', qdisc='pfifo_fast', state='up'),
                ipwrapper.Link(address='66:de:f1:da:aa:e7', index=419,
                               linkType=ipwrapper.LinkType.BOND, mtu=1500,
                               name='jbond', qdisc='pfifo_fast', state='up')]

    def testNics(self):
        """
        managed by vdsm: em, me, fake0, fake1
        not managed due to hidden bond (jbond) enslavement: me0, me1
        not managed due to being hidden nics: hid0, hideous
        """
        with MonkeyPatchScope([(netinfo, 'getLinks',
                                self._testNics),
                               (ipwrapper, '_bondExists',
                                lambda x: x == 'jbond'),
                               (ipwrapper.Link, '_detectType',
                                partial(_fakeTypeDetection, ipwrapper.Link)),
                               (ipwrapper.Link, '_fakeNics', ['fake*']),
                               (ipwrapper.Link, '_hiddenBonds', ['jb*']),
                               (ipwrapper.Link, '_hiddenNics', ['hid*'])
                               ]):
            self.assertEqual(set(netinfo.nics()),
                             set(['em', 'me', 'fake', 'fake0']))

    @ValidateRunningAsRoot
    def testFakeNics(self):
        with MonkeyPatchScope([(ipwrapper.Link, '_fakeNics', ['veth_*',
                                                              'dummy_*'])]):
            with veth.pair() as (v1a, v1b):
                with dummy.device() as d1:
                    fakes = set([d1, v1a, v1b])
                    nics = netinfo.nics()
                    self.assertTrue(fakes.issubset(nics), 'Fake devices %s are'
                                    ' not listed in nics %s' % (fakes, nics))

            with veth.pair(prefix='mehv_') as (v2a, v2b):
                with dummy.device(prefix='mehd_') as d2:
                    hiddens = set([d2, v2a, v2b])
                    nics = netinfo.nics()
                    self.assertFalse(hiddens.intersection(nics), 'Some of '
                                     'hidden devices %s is shown in nics %s' %
                                     (hiddens, nics))

    def testGetIfaceCfg(self):
        deviceName = "___This_could_never_be_a_device_name___"
        ifcfg = ('GATEWAY0=1.1.1.1\n' 'NETMASK=255.255.0.0\n')
        with namedTemporaryDir() as tempDir:
            ifcfgPrefix = os.path.join(tempDir, 'ifcfg-')
            filePath = ifcfgPrefix + deviceName

            with MonkeyPatchScope([(netinfo, 'NET_CONF_PREF', ifcfgPrefix)]):
                with open(filePath, 'w') as ifcfgFile:
                    ifcfgFile.write(ifcfg)
                self.assertEqual(
                    netinfo.getIfaceCfg(deviceName)['GATEWAY'], '1.1.1.1')
                self.assertEqual(
                    netinfo.getIfaceCfg(deviceName)['NETMASK'], '255.255.0.0')

    def testGetDhclientIfaces(self):
        LEASES = (
            'lease {{\n'
            '  interface "valid";\n'
            '  expire {active_datetime:%w %Y/%m/%d %H:%M:%S};\n'
            '}}\n'
            'lease {{\n'
            '  interface "valid2";\n'
            '  expire epoch {active:.0f}; # Sat Jan 31 20:04:20 2037\n'
            '}}\n'                   # human-readable date is just a comment
            'lease {{\n'
            '  interface "valid3";\n'
            '  expire never;\n'
            '}}\n'
            'lease {{\n'
            '  interface "expired";\n'
            '  expire {expired_datetime:%w %Y/%m/%d %H:%M:%S};\n'
            '}}\n'
            'lease {{\n'
            '  interface "expired2";\n'
            '  expire epoch {expired:.0f}; # Fri Jan 31 20:04:20 2014\n'
            '}}\n'
            'lease6 {{\n'
            '  interface "valid4";\n'
            '  ia-na [some MAC address] {{\n'
            '    iaaddr [some IPv6 address] {{\n'
            '      starts {now:.0f};\n'
            '      max-life 60;\n'  # the lease has a minute left
            '    }}\n'
            '  }}\n'
            '}}\n'
            'lease6 {{\n'
            '  interface "expired3";\n'
            '  ia-na [some MAC address] {{\n'
            '    iaaddr [some IPv6 address] {{\n'
            '      starts {expired:.0f};\n'
            '      max-life 30;\n'  # the lease expired half a minute ago
            '    }}\n'
            '  }}\n'
            '}}\n'
        )

        with namedTemporaryDir() as tmp_dir:
            lease_file = os.path.join(tmp_dir, 'test.lease')
            with open(lease_file, 'w') as f:
                now = time.time()
                last_minute = now - 60
                next_minute = now + 60

                f.write(LEASES.format(
                    active_datetime=datetime.utcfromtimestamp(next_minute),
                    active=next_minute,
                    expired_datetime=datetime.utcfromtimestamp(last_minute),
                    expired=last_minute,
                    now=now
                ))

            dhcpv4_ifaces, dhcpv6_ifaces = \
                netinfo._get_dhclient_ifaces([lease_file])

        self.assertIn('valid', dhcpv4_ifaces)
        self.assertIn('valid2', dhcpv4_ifaces)
        self.assertIn('valid3', dhcpv4_ifaces)
        self.assertNotIn('expired', dhcpv4_ifaces)
        self.assertNotIn('expired2', dhcpv4_ifaces)
        self.assertIn('valid4', dhcpv6_ifaces)
        self.assertNotIn('expired3', dhcpv6_ifaces)

    @brokentest("Skipped becasue it breaks randomly on the CI")
    @MonkeyPatch(netinfo, 'BONDING_DEFAULTS', netinfo.BONDING_DEFAULTS
                 if os.path.exists(netinfo.BONDING_DEFAULTS)
                 else 'bonding-defaults.json')
    @ValidateRunningAsRoot
    @RequireBondingMod
    def testGetBondingOptions(self):
        INTERVAL = '12345'
        bondName = random_iface_name()

        with open(netinfo.BONDING_MASTERS, 'w') as bonds:
            bonds.write('+' + bondName)
            bonds.flush()

            try:  # no error is anticipated but let's make sure we can clean up
                self.assertEqual(
                    netinfo._getBondingOptions(bondName), {}, "This test fails"
                    " when a new bonding option is added to the kernel. Please"
                    " run vdsm-tool dump-bonding-defaults` and retest.")

                with open(netinfo.BONDING_OPT % (bondName, 'miimon'),
                          'w') as opt:
                    opt.write(INTERVAL)

                self.assertEqual(netinfo._getBondingOptions(bondName),
                                 {'miimon': INTERVAL})

            finally:
                bonds.write('-' + bondName)

    def test_get_gateway(self):
        TEST_IFACE = 'test_iface'
        # different tables but the gateway is the same so it should be reported
        DUPLICATED_GATEWAY = {TEST_IFACE: [
            {
                'destination': 'none',
                'family': 'inet',
                'gateway': '12.34.56.1',
                'oif': TEST_IFACE,
                'oif_index': 8,
                'scope': 'global',
                'source': None,
                'table': 203569230,  # lucky us, we got the address 12.34.56.78
            }, {
                'destination': 'none',
                'family': 'inet',
                'gateway': '12.34.56.1',
                'oif': TEST_IFACE,
                'oif_index': 8,
                'scope': 'global',
                'source': None,
                'table': 254,
            }]}
        SINGLE_GATEWAY = {TEST_IFACE: [DUPLICATED_GATEWAY[TEST_IFACE][0]]}

        gateway = netinfo._get_gateway(SINGLE_GATEWAY, TEST_IFACE)
        self.assertEqual(gateway, '12.34.56.1')
        gateway = netinfo._get_gateway(DUPLICATED_GATEWAY, TEST_IFACE)
        self.assertEqual(gateway, '12.34.56.1')
