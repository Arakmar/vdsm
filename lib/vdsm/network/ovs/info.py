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

import six

from vdsm.network.netinfo.addresses import (
    getIpAddrs, getIpInfo, is_ipv6_local_auto)
from vdsm.network.netinfo.dhcp import dhcp_status
from vdsm.network.netinfo.mtus import getMtu
from vdsm.network.netinfo.routes import get_routes, get_gateway

from . import driver


NORTHBOUND = 'northbound'
SOUTHBOUND = 'southbound'

EMPTY_PORT_INFO = {
    'mtu': 1500,
    'addr': '',
    'ipv4addrs': [],
    'gateway': '',
    'netmask': '',
    'dhcpv4': False,
    'ipv6addrs': [],
    'ipv6autoconf': False,
    'ipv6gateway': '',
    'dhcpv6': False
}

SHARED_NETWORK_ATTRIBUTES = [
    'mtu', 'addr', 'ipv4addrs', 'gateway', 'netmask', 'dhcpv4', 'ipv6addrs',
    'ipv6autoconf', 'ipv6gateway', 'dhcpv6']


class OvsDB(object):
    def __init__(self, ovsdb):
        bridges_command = ovsdb.list_bridge_info()
        ports_command = ovsdb.list_port_info()
        ifaces_command = ovsdb.list_interface_info()

        with ovsdb.transaction() as transaction:
            transaction.add(bridges_command)
            transaction.add(ports_command)
            transaction.add(ifaces_command)

        self.bridges = bridges_command.result
        self.ports = ports_command.result
        self.ifaces = ifaces_command.result


class OvsInfo(object):
    def __init__(self):
        ovs_db = OvsDB(driver.create())
        self._ports_uuids = {port['_uuid']: port for port in ovs_db.ports}
        self._ifaces_uuids = {iface['_uuid']: iface for iface in ovs_db.ifaces}
        self._ifaces_macs = {iface['mac_in_use']: iface
                             for iface in ovs_db.ifaces if iface['mac_in_use']}

        self._bridges = {bridge['name']: self._bridge_attr(bridge)
                         for bridge in ovs_db.bridges}
        self._bridges_by_sb = self._get_bridges_by_sb()

    @property
    def bridges(self):
        return self._bridges

    @property
    def bridges_by_sb(self):
        return self._bridges_by_sb

    def _get_bridges_by_sb(self):
        bridges_by_sb = {}

        for bridge, attrs in six.iteritems(self.bridges):
            bridge_sb = self.southbound_port(attrs['ports'])
            bridges_by_sb[bridge_sb] = bridge

        return bridges_by_sb

    def _bridge_attr(self, bridge_entry):
        stp = bridge_entry['stp_enable']
        ports = [self._ports_uuids[uuid] for uuid in bridge_entry['ports']]
        ports_info = {port['name']: self._port_attr(port)
                      for port in ports}

        return {'ports': ports_info, 'stp': stp}

    def _port_attr(self, port_entry):
        bond_info = (self._bond_info(port_entry) if self._is_bond(port_entry)
                     else None)
        tag = port_entry['tag']
        level = port_entry['other_config'].get('vdsm_level')

        return {'bond': bond_info, 'tag': tag, 'level': level}

    @staticmethod
    def _is_bond(port_entry):
        """
        OVS implicitly defines a port as bond when it has two or more
        interfaces set on it.
        """
        return len(port_entry['interfaces']) >= 2

    def _bond_info(self, port_entry):
        slaves = sorted([self._ifaces_uuids[uuid]['name']
                         for uuid in port_entry['interfaces']])
        active_slave = self._ifaces_macs.get(port_entry['bond_active_slave'])
        fake_iface = port_entry['bond_fake_iface']
        mode = port_entry['bond_mode']
        lacp = port_entry['lacp']

        return {'slaves': slaves, 'active_slave': active_slave,
                'fake_iface': fake_iface, 'mode': mode, 'lacp': lacp}

    @staticmethod
    def southbound_port(ports):
        return next((port for port, attrs in six.iteritems(ports)
                     if attrs['level'] == SOUTHBOUND), None)

    @staticmethod
    def northbound_ports(ports):
        return (port for port, attrs in six.iteritems(ports)
                if attrs['level'] == NORTHBOUND)

    @staticmethod
    def bonds(ports):
        return ((port, attrs['bond']) for port, attrs in six.iteritems(ports)
                if attrs['bond'])


def get_netinfo():
    netinfo = _get_netinfo(OvsInfo())
    netinfo.update(_fake_devices(netinfo['networks']))
    return netinfo


def _fake_devices(networks):
    fake_devices = {'bridges': {}, 'vlans': {}}

    for net, attrs in six.iteritems(networks):
        fake_devices['bridges'][net] = _fake_bridge(attrs)
        vlanid = attrs.get('vlanid')
        if vlanid is not None:
            fake_devices['vlans'].update(_fake_vlan(attrs, vlanid))

    return fake_devices


def _fake_bridge(net_attrs):
    bridge_info = {
        'ports': net_attrs['ports'],
        'stp': net_attrs['stp']
    }
    bridge_info.update(_shared_net_attrs(net_attrs))
    return bridge_info


def _fake_vlan(net_attrs, vlanid):
    iface = net_attrs['bond'] or net_attrs['nics'][0]
    vlan_info = {
        'vlanid': vlanid,
        'iface': iface
    }
    vlan_info.update(EMPTY_PORT_INFO)
    vlan_name = '%s.%s' % (iface, vlanid)
    return {vlan_name: vlan_info}


def _get_netinfo(ovs_info):
    addresses = getIpAddrs()
    routes = get_routes()

    _netinfo = {'networks': {}, 'bondings': {}}

    for bridge, bridge_attrs in six.iteritems(ovs_info.bridges):
        ports = bridge_attrs['ports']

        southbound = ovs_info.southbound_port(ports)

        # northbound ports represents networks
        stp = bridge_attrs['stp']
        for northbound_port in ovs_info.northbound_ports(ports):
            _netinfo['networks'][northbound_port] = _get_network_info(
                northbound_port, bridge, southbound, ports, stp, addresses,
                routes)

        for bond, bond_attrs in ovs_info.bonds(ports):
            _netinfo['bondings'][bond] = _get_bond_info(bond_attrs)

    return _netinfo


def _get_network_info(northbound, bridge, southbound, ports, stp, addresses,
                      routes):
    southbound_bond_attrs = ports[southbound]['bond']
    bond = southbound if southbound_bond_attrs else ''
    nics = (southbound_bond_attrs['slaves'] if southbound_bond_attrs
            else [southbound])
    tag = ports[northbound]['tag']
    network_info = {
        'iface': northbound,
        'bridged': True,
        'bond': bond,
        'nics': nics,
        'ports': _get_net_ports(bridge, northbound, southbound, tag, ports),
        'stp': stp,
        'switch': 'ovs'
    }
    if tag is not None:
        # TODO: We should always report vlan, even if it is None. Netinfo
        # should be canonicalized before passed to caps, so None will not be
        # exposed in API call result.
        network_info['vlanid'] = tag
    network_info.update(_get_iface_info(northbound, addresses, routes))
    return network_info


def _get_net_ports(bridge, northbound, southbound, net_tag, ports):
    if net_tag:
        net_southbound_port = '{}.{}'.format(southbound, net_tag)
    else:
        net_southbound_port = southbound

    net_ports = [net_southbound_port]
    net_ports += [port for port, port_attrs in six.iteritems(ports)
                  if (port_attrs['tag'] == net_tag and port != bridge and
                      port_attrs['level'] not in (SOUTHBOUND, NORTHBOUND))]

    return net_ports


def _get_bond_info(bond_attrs):
    bond_info = {
        'slaves': bond_attrs['slaves'],
        # TODO: what should we report when no slave is active?
        'active_slave': (bond_attrs['active_slave'] or
                         bond_attrs['slaves'][0]),
        'opts': _to_bond_opts(bond_attrs['mode'], bond_attrs['lacp']),
        'switch': 'ovs'
    }
    bond_info.update(EMPTY_PORT_INFO)
    return bond_info


def _to_bond_opts(mode, lacp):
    custom_opts = []
    if mode:
        custom_opts.append('ovs_mode:%s' % mode)
    if lacp:
        custom_opts.append('ovs_lacp:%s' % lacp)
    return {'custom': ','.join(custom_opts)} if custom_opts else {}


def _get_iface_info(iface, addresses, routes):
    ipv4gateway = get_gateway(routes, iface, family=4)
    ipv4addr, ipv4netmask, ipv4addrs, ipv6addrs = getIpInfo(
        iface, addresses, ipv4gateway)
    is_dhcpv4, is_dhcpv6 = dhcp_status(iface, addresses)

    return {'mtu': getMtu(iface), 'addr': ipv4addr, 'ipv4addrs': ipv4addrs,
            'gateway': ipv4gateway, 'netmask': ipv4netmask,
            'dhcpv4': is_dhcpv4, 'ipv6addrs': ipv6addrs,
            'ipv6gateway': get_gateway(routes, iface, family=6),
            'ipv6autoconf': is_ipv6_local_auto(iface), 'dhcpv6': is_dhcpv6}


def fake_bridgeless(ovs_netinfo, nic_netinfo, running_bridgeless_networks):
    """
    An OVS setup does not support bridgeless networks. Requested bridgeless
    networks (as seen in running_config) are faked to appear as if they are
    bridgeless. Faking involves modifying the netinfo report, removing the
    faked bridge and creating the faked device that replaces it (vlan, bond
    or a nic).
    """
    for net in running_bridgeless_networks:
        net_attrs = ovs_netinfo['networks'][net]
        iface_type, iface_name = _bridgeless_fake_iface(net_attrs)

        if iface_type == 'nics':
            nic_netinfo[iface_name].update(_shared_net_attrs(net_attrs))
        else:
            ovs_netinfo[iface_type][iface_name].update(
                _shared_net_attrs(net_attrs))

        ovs_netinfo['networks'][net]['iface'] = iface_name

        ovs_netinfo['bridges'].pop(net)
        ovs_netinfo['networks'][net]['bridged'] = False


def _bridgeless_fake_iface(net_attrs):
    vlanid = net_attrs.get('vlanid')
    bond = net_attrs['bond']
    nics = net_attrs['nics']

    if vlanid is not None:
        iface_type = 'vlans'
        iface_name = '{}.{}'.format(bond or nics[0], vlanid)
    elif bond:
        iface_type = 'bondings'
        iface_name = bond
    else:
        iface_type = 'nics'
        iface_name = nics[0]

    return iface_type, iface_name


def _shared_net_attrs(attrs):
    return {key: attrs[key] for key in SHARED_NETWORK_ATTRIBUTES}
