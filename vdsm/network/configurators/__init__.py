# Copyright 2013-2014 Red Hat, Inc.
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
import ConfigParser
import logging

from vdsm.config import config
from vdsm.netconfpersistence import RunningConfig
from vdsm import ipwrapper
from vdsm import netinfo
from vdsm.netlink import monitor

from .dhclient import DhcpClient
from ..errors import ConfigNetworkError, ERR_FAILED_IFUP
from . import qos
from ..models import Bond, Bridge
from ..sourceroute import StaticSourceRoute


class RollbackIncomplete(Exception):
    pass


class Configurator(object):
    def __init__(self, configApplier, inRollback=False):
        self.configApplier = configApplier
        self._inRollback = inRollback
        self._libvirtAdded = set()

    def __enter__(self):
        self.begin()
        return self

    def __exit__(self, type, value, traceback):
        if type is None:
            self.commit()
        elif self._inRollback:
            # If we failed the rollback transaction, the networking system
            # is in no good state and we fail hard
            logging.error('Failed rollback transaction last known good '
                          'network.', exc_info=(type, value, traceback))
        else:
            leftover = self.rollback()
            if leftover:
                raise RollbackIncomplete(leftover, type, value)

    def rollback(self):
        """
        returns None when all the nets were successfully rolled back, a
        vdsm.netoconfpersistence.Config object with the not yet rolled back
        networks and bonds.
        """
        # self.runningConfig will have all the changes that were applied before
        # we needed to rollback.
        return RunningConfig().diffFrom(self.runningConfig)

    def configureBridge(self, bridge, **opts):
        raise NotImplementedError

    def configureVlan(self, vlan, **opts):
        raise NotImplementedError

    def configureBond(self, bond, **opts):
        raise NotImplementedError

    def editBonding(self, bond, _netinfo):
        raise NotImplementedError

    def configureNic(self, nic, **opts):
        raise NotImplementedError

    def removeBridge(self, bridge):
        raise NotImplementedError

    def removeVlan(self, vlan):
        raise NotImplementedError

    def removeBond(self, bonding):
        raise NotImplementedError

    def removeNic(self, nic):
        raise NotImplementedError

    def configureSourceRoute(self, routes, rules, device):
        raise NotImplementedError

    def removeSourceRoute(self, routes, rules, device):
        raise NotImplementedError

    def configureLibvirtNetwork(self, network, iface):
        self.configApplier.createLibvirtNetwork(network,
                                                isinstance(iface, Bridge),
                                                iface.name)
        self._libvirtAdded.add(network)

    def removeLibvirtNetwork(self, network):
        self.configApplier.removeLibvirtNetwork(network)

    def configureQoS(self, hostQos, top_device):
        out = hostQos.get('out')
        if out is not None:
            qos.configure_outbound(out, top_device)

    def removeQoS(self, top_device):
        qos.remove_outbound(top_device)

    def _addSourceRoute(self, netEnt):
        ipv4 = netEnt.ipv4
        # bootproto is None for both static and no bootproto
        if ipv4.bootproto != 'dhcp' and netEnt.master is None:
            logging.debug("Adding source route: name=%s, addr=%s, netmask=%s, "
                          "gateway=%s" % (netEnt.name, ipv4.address,
                                          ipv4.netmask, ipv4.gateway))
            if (ipv4.gateway in (None, '0.0.0.0')
               or not ipv4.address or not ipv4.netmask):
                    logging.warning(
                        'invalid input for source routing: name=%s, '
                        'addr=%s, netmask=%s, gateway=%s',
                        netEnt.name, ipv4.address, ipv4.netmask,
                        ipv4.gateway)
            else:
                StaticSourceRoute(netEnt.name, self, ipv4.address,
                                  ipv4.netmask, ipv4.gateway).configure()

    def _removeSourceRoute(self, netEnt, sourceRouteClass):
        if netEnt.ipv4.bootproto != 'dhcp' and netEnt.master is None:
            logging.debug("Removing source route for device %s", netEnt.name)
            sourceRouteClass(netEnt.name, self, None, None, None).remove()

    def _setNewMtu(self, iface, ifaceVlans):
        """
        Update an interface's MTU when one of its users is removed.

        :param iface: interface object (bond or nic device)
        :type iface: NetDevice instance

        :param ifaceVlans: vlan devices using the interface 'iface'
        :type ifaceVlans: iterable

        :return mtu value that was applied
        """
        ifaceMtu = netinfo.getMtu(iface.name)
        maxMtu = netinfo.getMaxMtu(ifaceVlans, None)
        if maxMtu and maxMtu < ifaceMtu:
            if isinstance(iface, Bond):
                self.configApplier.setBondingMtu(iface.name, maxMtu)
            else:
                self.configApplier.setIfaceMtu(iface.name, maxMtu)
        return maxMtu


def getEthtoolOpts(name):
    try:
        opts = config.get('vars', 'ethtool_opts.' + name)
    except ConfigParser.NoOptionError:
        opts = config.get('vars', 'ethtool_opts')
    return opts


def runDhclient(iface, family=4, default_route=False):
    dhclient = DhcpClient(iface.name, family, default_route, iface.duid_source)
    rc, _, _ = dhclient.start(iface.blockingdhcp)
    if iface.blockingdhcp and rc:
        raise ConfigNetworkError(ERR_FAILED_IFUP, 'dhclient%s failed' % family)


def wait_for_device(name, timeout=1):
    """
    Wait for a network device to appear in a given timeout. If the device is
    not created by then, raise a ConfigNetworkError.
    """
    with monitor.Monitor(timeout=timeout, groups=('link',),
                         silent_timeout=True) as mon:
        if name in (link.name for link in ipwrapper.getLinks()):
            return
        for event in mon:
            if event.get('name') == name and event.get('event') == 'new_link':
                return
    raise ConfigNetworkError(ERR_FAILED_IFUP, 'Device %s was not created '
                             'during a %ss timeout.' % (name, timeout))
