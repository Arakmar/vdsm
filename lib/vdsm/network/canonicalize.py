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

from .netinfo import bridges, mtus, bonding, dns
from vdsm import utils

from .errors import ConfigNetworkError
from . import errors as ne


def canonicalize_networks(nets):
    """
    Given networks configuration, explicitly add missing defaults.
    :param nets: The network configuration
    """
    for attrs in six.itervalues(nets):
        # If net is marked for removal, normalize the mark to boolean and
        # ignore all other attributes canonization.
        if _canonicalize_remove(attrs):
            continue

        _canonicalize_mtu(attrs)
        _canonicalize_vlan(attrs)
        _canonicalize_bridged(attrs)
        _canonicalize_stp(attrs)
        _canonicalize_ipv6(attrs)
        _canonicalize_switch_type_net(attrs)
        _canonicalize_ip_default_route(attrs)
        _canonicalize_nameservers(attrs)


def canonicalize_bondings(bonds):
    """
    Given bondings configuration, explicitly add missing defaults.
    :param bonds: The bonding configuration
    """
    for attrs in six.itervalues(bonds):
        # If bond is marked for removal, normalize the mark to boolean and
        # ignore all other attributes canonization.
        if _canonicalize_remove(attrs):
            continue

        _canonicalize_switch_type_bond(attrs)


def _canonicalize_remove(data):
    if 'remove' in data:
        data['remove'] = utils.tobool(data['remove'])
        return data['remove']
    return False


def _canonicalize_mtu(data):
    data['mtu'] = int(data['mtu']) if 'mtu' in data else mtus.DEFAULT_MTU


def _canonicalize_vlan(data):
    vlan = data.get('vlan', None)
    if vlan in (None, ''):
        data.pop('vlan', None)
    else:
        data['vlan'] = int(vlan)


def _canonicalize_bridged(data):
    if 'bridged' in data:
        data['bridged'] = utils.tobool(data['bridged'])
    else:
        data['bridged'] = True


def _canonicalize_stp(data):
    if data['bridged']:
        stp = False
        if 'stp' in data:
            stp = data['stp']
        elif 'STP' in data:
            stp = data.pop('STP')
        try:
            data['stp'] = bridges.stp_booleanize(stp)
        except ValueError:
            raise ConfigNetworkError(ne.ERR_BAD_PARAMS, '"%s" is not '
                                     'a valid bridge STP value.' % stp)


def _canonicalize_ipv6(data):
    if 'dhcpv6' not in data:
        data['dhcpv6'] = False
    if 'ipv6autoconf' not in data:
        data['ipv6autoconf'] = False


def _canonicalize_switch_type_net(data):
    if utils.tobool(utils.rget(data, ('custom', 'ovs'))):
        data['switch'] = 'ovs'
    elif 'switch' not in data:
        data['switch'] = 'legacy'


def _canonicalize_switch_type_bond(data):
    options = data.get('options', '')
    ovs = utils.rget(bonding.parse_bond_options(options), ('custom', 'ovs'))
    if utils.tobool(ovs):
        data['switch'] = 'ovs'
    elif 'switch' not in data:
        data['switch'] = 'legacy'


def _canonicalize_ip_default_route(data):
    if 'defaultRoute' not in data:
        data['defaultRoute'] = False


def _canonicalize_nameservers(data):
    if 'nameservers' not in data:
        # Nameservers are relevant only for the management network.
        if data['defaultRoute']:
            data['nameservers'] = dns.get_host_nameservers()
        else:
            data['nameservers'] = []
