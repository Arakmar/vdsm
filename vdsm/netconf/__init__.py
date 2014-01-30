# Copyright 2013 Red Hat, Inc.
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

import ConfigParser
import logging

import libvirtCfg
from netmodels import Bond, Bridge
from sourceRoute import DynamicSourceRoute
from sourceRoute import StaticSourceRoute
from vdsm import netinfo
from vdsm.config import config
from vdsm.netconfpersistence import RunningConfig


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
                          'network. ERR=%s', exc_info=(type, value, traceback))
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

    def flush(self):
        libvirtCfg.flush()

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

    def configureLibvirtNetwork(self, network, iface, qosInbound=None,
                                qosOutbound=None):
        self.configApplier.createLibvirtNetwork(network,
                                                isinstance(iface, Bridge),
                                                iface.name,
                                                qosInbound=qosInbound,
                                                qosOutbound=qosOutbound)
        self._libvirtAdded.add(network)

    def removeLibvirtNetwork(self, network):
        self.configApplier.removeLibvirtNetwork(network)

    def _addSourceRoute(self, netEnt):
        ip = netEnt.ipConfig
        # bootproto is None for both static and no bootproto
        if ip.bootproto != 'dhcp' and netEnt.master is None:
            logging.debug("Adding source route %s, %s, %s, %s" %
                          (netEnt.name, ip.ipaddr, ip.netmask, ip.gateway))
            StaticSourceRoute(netEnt.name, self).\
                configure(ip.ipaddr, ip.netmask, ip.gateway)
        DynamicSourceRoute.addInterfaceTracking(netEnt)

    def _removeSourceRoute(self, netEnt):
        if netEnt.ipConfig.bootproto != 'dhcp' and netEnt.master is None:
            logging.debug("Removing source route for device %s" % netEnt.name)
            StaticSourceRoute(netEnt.name, self).remove()

    def _setNewMtu(self, iface, ifaceVlans):
        """
        Update an interface's MTU when one of its users is removed.

        :param iface: interface object (bond or nic device)
        :type iface: NetDevice instance

        :param ifaceVlans: vlan devices using the interface 'iface'
        :type ifaceVlans: iterable

        """
        ifaceMtu = netinfo.getMtu(iface.name)
        maxMtu = netinfo.getMaxMtu(ifaceVlans, None)
        if maxMtu and maxMtu < ifaceMtu:
            if isinstance(iface, Bond):
                self.configApplier.setBondingMtu(iface.name, maxMtu)
            else:
                self.configApplier.setIfaceMtu(iface.name, maxMtu)


def getEthtoolOpts(name):
    try:
        opts = config.get('vars', 'ethtool_opts.' + name)
    except ConfigParser.NoOptionError:
        opts = config.get('vars', 'ethtool_opts')
    return opts
