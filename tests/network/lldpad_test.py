#
# Copyright 2017 Red Hat, Inc.
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

from nose.plugins.attrib import attr
from nose.plugins.skip import SkipTest

from vdsm.network.link import iface
from vdsm.network.lldpad import lldptool

from testlib import VdsmTestCase, mock

from .nettestlib import veth_pair
from .nettestlib import enable_lldp_on_ifaces
from .nettestlib import requires_systemctl


LLDP_CHASSIS_ID_TLV = 'Chassis ID TLV\n\tMAC: 01:23:45:67:89:ab'

LLDP_MULTIPLE_TLVS = """
Chassis ID TLV
\tMAC: 01:23:45:67:89:ab
Port ID TLV
\tLocal: 588
Time to Live TLV
\t120
System Name TLV
\tsite1-row2-rack3
System Description TLV
\tmanufacturer, Build date: 2016-01-20 05:03:06 UTC
System Capabilities TLV
\tSystem capabilities:  Bridge, Router
\tEnabled capabilities: Bridge, Router
Management Address TLV
\tIPv4: 10.21.0.40
\tIfindex: 36
\tOID: $
Port Description TLV
\tsome important server, port 4
MAC/PHY Configuration Status TLV
\tAuto-negotiation supported and enabled
\tPMD auto-negotiation capabilities: 0x0001
\tMAU type: Unknown [0x0000]
Link Aggregation TLV
\tAggregation capable
\tCurrently aggregated
\tAggregated Port ID: 600
Maximum Frame Size TLV
\t9216
Port VLAN ID TLV
\tPVID: 2000
VLAN Name TLV
\tVID 2000: Name foo
VLAN Name TLV
\tVID 2001: Name bar
LLDP-MED Capabilities TLV
\tDevice Type:  netcon
\tCapabilities: LLDP-MED, Network Policy, Location Identification, '
\tExtended Power via MDI-PSE
Unidentified Org Specific TLV
\tOUI: 0x009069, Subtype: 1, Info: 504533373135323130333833
End of LLDPDU TLV
"""


@attr(type='unit')
class LldpadReportTests(VdsmTestCase):
    TLVS_REPORT = [
        {'type': 1, 'name': 'Chassis ID',
         'properties': {'chassis ID': '01:23:45:67:89:ab',
                        'chassis ID subtype': 'MAC'}},
        {'type': 2, 'name': 'Port ID',
         'properties': {'port ID': '588', 'port ID subtype': 'Local'}},
        {'type': 3, 'name': 'Time to Live',
         'properties': {'time to live': '120'}},
        {'type': 5, 'name': 'System Name',
         'properties': {'system name': 'site1-row2-rack3'}},
        {'type': 6, 'name': 'System Description', 'properties': {
            'system description':
                'manufacturer, Build date: 2016-01-20 05:03:06 UTC'}},
        {'type': 7, 'name': 'System Capabilities',
         'properties': {'system capabilities': 'Bridge, Router',
                        'enabled capabilities': 'Bridge, Router'}},
        {'type': 8, 'name': 'Management Address',
         'properties': {'object identifier': '$',
                        'interface numbering subtype': 'Ifindex',
                        'interface numbering': '36',
                        'management address subtype': 'IPv4',
                        'management address': '10.21.0.40'}},
        {'type': 4, 'name': 'Port Description',
         'properties': {'port description': 'some important server, port 4'}},
        {'subtype': 1, 'oui': 32962, 'type': 127, 'name': 'Port VLAN ID',
         'properties': {'Port VLAN ID': '2000'}},
        {'subtype': 3, 'oui': 32962, 'type': 127, 'name': 'VLAN Name',
         'properties': {'VLAN ID': 'Name foo', 'VLAN Name': '2000'}},
        {'subtype': 3, 'oui': 32962, 'type': 127, 'name': 'VLAN Name',
         'properties': {'VLAN ID': 'Name bar', 'VLAN Name': '2001'}}]

    @mock.patch.object(lldptool.commands, 'execCmd',
                       lambda command, raw: (0, LLDP_CHASSIS_ID_TLV, ''))
    def test_get_single_lldp_tlv(self):
        expected = [self.TLVS_REPORT[0]]
        self.assertEqual(expected, lldptool.get_tlvs('iface0'))

    @mock.patch.object(lldptool.commands, 'execCmd',
                       lambda command, raw: (0, LLDP_MULTIPLE_TLVS, ''))
    def test_get_multiple_lldp_tlvs(self):
        self.assertEqual(self.TLVS_REPORT, lldptool.get_tlvs('iface0'))


@attr(type='integration')
class LldpadReportIntegTests(VdsmTestCase):

    @requires_systemctl
    def setUp(self):
        if not lldptool.is_lldpad_service_running():
            raise SkipTest('LLDPAD service is not running.')

    def test_get_lldp_tlvs(self):
        with veth_pair() as (nic1, nic2):
            iface.up(nic1)
            iface.up(nic2)
            with enable_lldp_on_ifaces((nic1, nic2), rx_only=False):
                self.assertTrue(lldptool.is_lldp_enabled_on_iface(nic1))
                self.assertTrue(lldptool.is_lldp_enabled_on_iface(nic2))
                tlvs = lldptool.get_tlvs(nic1)
                self.assertEqual(3, len(tlvs))
                expected_ttl_tlv = {
                    'type': 3,
                    'name': 'Time to Live',
                    'properties': {
                        'time to live': '120'
                    }
                }
                self.assertEqual(expected_ttl_tlv, tlvs[-1])

                tlvs = lldptool.get_tlvs(nic2)
                self.assertEqual(3, len(tlvs))
