# Copyright 2011-2014 Red Hat, Inc.
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
from __future__ import print_function
from functools import wraps
import errno
import inspect
import sys
import os
import traceback
import time
import logging

from vdsm.config import config
from vdsm import constants
from vdsm import netconfpersistence
from vdsm import netinfo
from vdsm import udevadm
from vdsm import utils
from vdsm import ipwrapper

from .configurators import libvirt
from .errors import ConfigNetworkError
from . import errors as ne
from .models import Bond, Bridge, IPv4, IPv6, Nic, Vlan
from .models import hierarchy_backing_device
import hooks  # TODO: Turn into parent package import when vdsm is a package

CONNECTIVITY_TIMEOUT_DEFAULT = 4
_SYSFS_SRIOV_NUMVFS = '/sys/bus/pci/devices/{}/sriov_numvfs'


def _getConfiguratorClass():
    configurator = config.get('vars', 'net_configurator')
    if configurator == 'iproute2':
        from .configurators.iproute2 import Iproute2
        return Iproute2
    elif configurator == 'pyroute2':
        try:
            from .configurators.pyroute_two import PyrouteTwo
            return PyrouteTwo
        except ImportError:
            logging.error('pyroute2 library for %s configurator is missing. '
                          'Use ifcfg instead.', configurator)
            from .configurator.ifcfg import Ifcfg
            return Ifcfg

    else:
        if configurator != 'ifcfg':
            logging.warn('Invalid config for network configurator: %s. '
                         'Use ifcfg instead.', configurator)
        from .configurators.ifcfg import Ifcfg
        return Ifcfg


def _getPersistenceModule():
    persistence = config.get('vars', 'net_persistence')
    if persistence == 'unified':
        return netconfpersistence
    else:
        from .configurators import ifcfg
        return ifcfg


ConfiguratorClass = _getConfiguratorClass()
persistence = _getPersistenceModule()


def _objectivizeNetwork(bridge=None, vlan=None, vlan_id=None, bonding=None,
                        bondingOptions=None, nics=None, mtu=None, ipaddr=None,
                        netmask=None, gateway=None, bootproto=None,
                        ipv6addr=None, ipv6gateway=None, ipv6autoconf=None,
                        dhcpv6=None, defaultRoute=False, _netinfo=None,
                        configurator=None, blockingdhcp=None,
                        implicitBonding=None, opts=None):
    """
    Constructs an object hierarchy that describes the network configuration
    that is passed in the parameters.

    :param bridge: name of the bridge.
    :param vlan: vlan device name.
    :param vlan_id: vlan tag id.
    :param bonding: name of the bond.
    :param bondingOptions: bonding options separated by spaces.
    :param nics: list of nic names.
    :param mtu: the desired network maximum transmission unit.
    :param ipaddr: IPv4 address in dotted decimal format.
    :param netmask: IPv4 mask in dotted decimal format.
    :param gateway: IPv4 address in dotted decimal format.
    :param bootproto: protocol for getting IP config for the net, e.g., 'dhcp'
    :param ipv6addr: IPv6 address in format address[/prefixlen].
    :param ipv6gateway: IPv6 address in format address[/prefixlen].
    :param ipv6autoconf: whether to use IPv6's stateless autoconfiguration.
    :param dhcpv6: whether to use DHCPv6.
    :param _netinfo: network information snapshot.
    :param configurator: instance to use to apply the network configuration.
    :param blockingdhcp: whether to acquire dhcp IP config in a synced manner.
    :param implicitBonding: whether the bond's existance is tied to it's
                            master's.
    :param defaultRoute: Should this network's gateway be set in the main
                         routing table?
    :param opts: misc options received by the callee, e.g., {'stp': True}. this
                 function can modify the dictionary.

    :returns: the top object of the hierarchy.
    """
    if configurator is None:
        configurator = ConfiguratorClass()
    if _netinfo is None:
        _netinfo = netinfo.NetInfo()
    if opts is None:
        opts = {}
    if bootproto == 'none':
        bootproto = None
    if bondingOptions and not bonding:
        raise ConfigNetworkError(ne.ERR_BAD_BONDING, 'Bonding options '
                                 'specified without bonding')
    topNetDev = None
    if bonding:
        topNetDev = Bond.objectivize(bonding, configurator, bondingOptions,
                                     nics, mtu, _netinfo, implicitBonding)
    elif nics:
        try:
            nic, = nics
        except ValueError:
            raise ConfigNetworkError(ne.ERR_BAD_BONDING, 'Multiple nics '
                                     'require a bonding device')
        else:
            bond = _netinfo.getBondingForNic(nic)
            if bond:
                raise ConfigNetworkError(ne.ERR_USED_NIC, 'nic %s already '
                                         'enslaved to %s' % (nic, bond))
            topNetDev = Nic(nic, configurator, mtu=mtu, _netinfo=_netinfo)
    if vlan is not None:
        tag = netinfo.getVlanID(vlan) if vlan_id is None else vlan_id
        topNetDev = Vlan(topNetDev, tag, configurator, mtu=mtu, name=vlan)
    elif vlan_id is not None:
        topNetDev = Vlan(topNetDev, vlan_id, configurator, mtu=mtu)
    if bridge is not None:
        stp = None
        if 'stp' in opts:
            stp = opts.pop('stp')
        elif 'STP' in opts:
            stp = opts.pop('STP')
        try:
            stp = netinfo.stp_booleanize(stp)
        except ValueError:
            raise ConfigNetworkError(ne.ERR_BAD_PARAMS, '"%s" is not a valid '
                                     'bridge STP value.' % stp)
        topNetDev = Bridge(bridge, configurator, port=topNetDev, mtu=mtu,
                           stp=stp)
        # inherit DUID from the port's existing DHCP lease (BZ#1219429)
        if topNetDev.port and bootproto == 'dhcp':
            _inherit_dhcp_unique_identifier(topNetDev, _netinfo)
    if topNetDev is None:
        raise ConfigNetworkError(ne.ERR_BAD_PARAMS, 'Network defined without '
                                 'devices.')
    topNetDev.ipv4 = IPv4(ipaddr, netmask, gateway, defaultRoute, bootproto)
    topNetDev.ipv6 = IPv6(ipv6addr, ipv6gateway, defaultRoute, ipv6autoconf,
                          dhcpv6)
    topNetDev.blockingdhcp = (configurator._inRollback or
                              utils.tobool(blockingdhcp))
    return topNetDev


def _validateInterNetworkCompatibility(ni, vlan, iface):
    iface_nets_by_vlans = dict((vlan, net)  # None is also a valid key
                               for (net, vlan)
                               in ni.getNetworksAndVlansForIface(iface))

    if vlan in iface_nets_by_vlans:
        raise ConfigNetworkError(ne.ERR_BAD_PARAMS,
                                 'interface %r cannot be defined with this '
                                 'network since it is already defined with '
                                 'network %s' % (iface,
                                                 iface_nets_by_vlans[vlan]))


def _alterRunningConfig(func):
    """Wrapper for _addNetwork and _delNetwork that abstracts away all current
    configuration handling from the wrapped methods."""
    spec = inspect.getargspec(func)

    @wraps(func)
    def wrapped(*args, **kwargs):
        if not config.get('vars', 'net_persistence') == 'unified':
            return func(*args, **kwargs)

        # Get args and kwargs in a single dictionary
        attrs = kwargs.copy()
        attrs.update(dict(zip(spec.args, args)))

        isolatedCommand = attrs.get('configurator') is None
        # Detect if we are running an isolated command, i.e., a command that is
        # not called as part of composed API operation like setupNetworks or
        # editNetwork, but rather as its own API verb. This is necessary in
        # order to maintain behavior of the addNetwork and delNetwork API
        # verbs
        if isolatedCommand:
            attrs['configurator'] = configurator = ConfiguratorClass()
            configurator.begin()
        else:
            configurator = attrs['configurator']

        ret = func(**attrs)

        nics = attrs.pop('nics', None)
        # Bond config handled in configurator so that operations only touching
        # bonds don't need special casing and the logic of this remains simpler
        if not attrs.get('bonding'):
            if nics:
                attrs['nic'], = nics

        if func.__name__ == '_delNetwork':
            configurator.runningConfig.removeNetwork(attrs.pop('network'))
        else:
            configurator.runningConfig.setNetwork(attrs.pop('network'), attrs)
        if isolatedCommand:  # Commit the no-rollback transaction.
            configurator.commit()
        return ret
    return wrapped


def _update_bridge_ports_mtu(bridge, mtu):
    for port in netinfo.ports(bridge):
        ipwrapper.linkSet(port, ['mtu', str(mtu)])


@_alterRunningConfig
def _addNetwork(network, vlan=None, bonding=None, nics=None, ipaddr=None,
                netmask=None, prefix=None, mtu=None, gateway=None, dhcpv6=None,
                ipv6addr=None, ipv6gateway=None, ipv6autoconf=None,
                force=False, configurator=None, bondingOptions=None,
                bridged=True, _netinfo=None, hostQos=None,
                defaultRoute=False, blockingdhcp=False, **options):
    nics = nics or ()
    if _netinfo is None:
        _netinfo = netinfo.NetInfo()
    bridged = utils.tobool(bridged)
    if dhcpv6 is not None:
        dhcpv6 = utils.tobool(dhcpv6)
    if ipv6autoconf is not None:
        ipv6autoconf = utils.tobool(ipv6autoconf)
    vlan = _vlanToInternalRepresentation(vlan)

    if mtu:
        mtu = int(mtu)

    if network == '':
        raise ConfigNetworkError(ne.ERR_BAD_BRIDGE,
                                 'Empty network names are not valid')
    if prefix:
        if netmask:
            raise ConfigNetworkError(ne.ERR_BAD_PARAMS,
                                     'Both PREFIX and NETMASK supplied')
        else:
            try:
                netmask = netinfo.prefix2netmask(int(prefix))
            except ValueError as ve:
                raise ConfigNetworkError(ne.ERR_BAD_ADDR, "Bad prefix: %s" %
                                         ve)

    if not utils.tobool(force):
        logging.debug('validating network...')
        if network in _netinfo.networks:
            raise ConfigNetworkError(
                ne.ERR_USED_BRIDGE, 'Network already exists (%s)' % (network,))
        if bonding:
            _validateInterNetworkCompatibility(_netinfo, vlan, bonding)
        else:
            for nic in nics:
                _validateInterNetworkCompatibility(_netinfo, vlan, nic)

    logging.info("Adding network %s with vlan=%s, bonding=%s, nics=%s,"
                 " bondingOptions=%s, mtu=%s, bridged=%s, defaultRoute=%s,"
                 "options=%s", network, vlan, bonding, nics, bondingOptions,
                 mtu, bridged, defaultRoute, options)

    if configurator is None:
        configurator = ConfiguratorClass()

    bootproto = options.pop('bootproto', None)

    net_ent = _objectivizeNetwork(
        bridge=network if bridged else None, vlan_id=vlan, bonding=bonding,
        bondingOptions=bondingOptions, nics=nics, mtu=mtu, ipaddr=ipaddr,
        netmask=netmask, gateway=gateway, bootproto=bootproto, dhcpv6=dhcpv6,
        blockingdhcp=blockingdhcp, ipv6addr=ipv6addr, ipv6gateway=ipv6gateway,
        ipv6autoconf=ipv6autoconf, defaultRoute=defaultRoute,
        _netinfo=_netinfo, configurator=configurator, opts=options)

    if bridged and network in _netinfo.bridges:
        net_ent_to_configure = net_ent.port
        logging.info("Bridge %s already exists.", network)
        if mtu:
            # The bridge already exists and we attach a new underlying device
            # to it. We need to make sure that the bridge MTU configuration is
            # updated.
            configurator.configApplier.setIfaceMtu(network, mtu)
            # We must also update the vms` tap devices (the bridge ports in
            # this case) so that their MTU is synced with the bridge
            _update_bridge_ports_mtu(net_ent.name, mtu)
    else:
        net_ent_to_configure = net_ent

    if net_ent_to_configure is not None:
        logging.info("Configuring device %s", net_ent_to_configure)
        net_ent_to_configure.configure(**options)
    configurator.configureLibvirtNetwork(network, net_ent)
    if hostQos is not None:
        configurator.configureQoS(hostQos, net_ent)


def assertBridgeClean(bridge, vlan, bonding, nics):
    ports = set(netinfo.ports(bridge))
    ifaces = set(nics)
    if vlan is not None:
        ifaces.add(vlan)
    else:
        ifaces.add(bonding)

    brifs = ports - ifaces

    if brifs:
        raise ConfigNetworkError(ne.ERR_USED_BRIDGE, 'bridge %s has interfaces'
                                 ' %s connected' % (bridge, brifs))


def showNetwork(network):
    _netinfo = netinfo.NetInfo()
    if network not in _netinfo.networks:
        print("Network %r doesn't exist" % network)
        return

    bridged = _netinfo.networks[network]['bridged']
    print("Network %s(Bridged: %s):" % (network, bridged))

    nics, vlan, vlan_id, bonding = _netinfo.getNicsVlanAndBondingForNetwork(
        network)

    if bridged:
        ipaddr = _netinfo.networks[network]['addr']
        netmask = _netinfo.networks[network]['netmask']
        gateway = _netinfo.networks[network]['gateway']
        print("ipaddr=%s, netmask=%s, gateway=%s" % (ipaddr, netmask, gateway))
    else:
        iface = _netinfo.networks[network]['iface']
        ipaddr = _netinfo.nics[iface]['addr']
        netmask = _netinfo.nics[iface]['netmask']
        print("ipaddr=%s, netmask=%s" % (ipaddr, netmask))

    print("vlan=%s, bonding=%s, nics=%s" % (vlan_id, bonding, nics))


def listNetworks():
    _netinfo = netinfo.NetInfo()
    print("Networks:", _netinfo.networks.keys())
    print("Vlans:", _netinfo.vlans.keys())
    print("Nics:", _netinfo.nics.keys())
    print("Bondings:", _netinfo.bondings.keys())


def _delBrokenNetwork(network, netAttr, configurator):
    '''Adapts the network information of broken networks so that they can be
    deleted via _delNetwork.'''
    _netinfo = netinfo.NetInfo()
    _netinfo.networks[network] = netAttr
    _netinfo.networks[network]['dhcpv4'] = False

    if _netinfo.networks[network]['bridged']:
        try:
            nets = configurator.runningConfig.networks
        except AttributeError:
            nets = {}  # ifcfg does not need net definitions
        _netinfo.networks[network]['ports'] = persistence.configuredPorts(
            nets, network)
    elif not os.path.exists('/sys/class/net/' + netAttr['iface']):
        # Bridgeless broken network without underlying device
        libvirt.removeNetwork(network)
        configurator.runningConfig.removeNetwork(network)
        return
    _delNetwork(network, configurator=configurator, force=True,
                implicitBonding=False, _netinfo=_netinfo)


def _validateDelNetwork(network, vlan, bonding, nics, bridge_should_be_clean,
                        _netinfo):
    if bonding:
        if set(nics) != set(_netinfo.bondings[bonding]["slaves"]):
            raise ConfigNetworkError(ne.ERR_BAD_NIC, '_delNetwork: %s are '
                                     'not all nics enslaved to %s' %
                                     (nics, bonding))
    if bridge_should_be_clean:
        assertBridgeClean(network, vlan, bonding, nics)


def _delNonVdsmNetwork(network, vlan, bonding, nics, _netinfo, configurator):
    if network in netinfo.bridges():
        net_ent = _objectivizeNetwork(bridge=network, vlan_id=vlan,
                                      bonding=bonding, nics=nics,
                                      _netinfo=_netinfo,
                                      configurator=configurator,
                                      implicitBonding=False)
        net_ent.remove()
    else:
        raise ConfigNetworkError(ne.ERR_BAD_BRIDGE, "Cannot delete network"
                                 " %r: It doesn't exist in the system" %
                                 network)


def _disconnect_bridge_port(port):
    ipwrapper.linkSet(port, ['nomaster'])


@_alterRunningConfig
def _delNetwork(network, vlan=None, bonding=None, nics=None, force=False,
                configurator=None, implicitBonding=True, _netinfo=None,
                keep_bridge=False, **options):
    if _netinfo is None:
        _netinfo = netinfo.NetInfo()

    if configurator is None:
        configurator = ConfiguratorClass()

    if network not in _netinfo.networks:
        logging.info("Network %r: doesn't exist in libvirt database", network)
        vlan = _vlanToInternalRepresentation(vlan)
        _delNonVdsmNetwork(network, vlan, bonding, nics, _netinfo,
                           configurator)
        return

    nics, vlan, vlan_id, bonding = _netinfo.getNicsVlanAndBondingForNetwork(
        network)
    bridged = _netinfo.networks[network]['bridged']

    logging.info("Removing network %s with vlan=%s, bonding=%s, nics=%s,"
                 "keep_bridge=%s options=%s", network, vlan, bonding,
                 nics, keep_bridge, options)

    if not utils.tobool(force):
        _validateDelNetwork(network, vlan, bonding, nics,
                            bridged and not keep_bridge, _netinfo)

    net_ent = _objectivizeNetwork(bridge=network if bridged else None,
                                  vlan=vlan, vlan_id=vlan_id, bonding=bonding,
                                  nics=nics, _netinfo=_netinfo,
                                  configurator=configurator,
                                  implicitBonding=implicitBonding)
    net_ent.ipv4.bootproto = (
        'dhcp' if _netinfo.networks[network]['dhcpv4'] else 'none')

    if bridged and keep_bridge:
        # we now leave the bridge intact but delete everything underneath it
        net_ent_to_remove = net_ent.port
        if net_ent_to_remove is not None:
            # the configurator will not allow us to remove a bridge interface
            # (be it vlan, bond or nic) unless it is not used anymore. Since
            # we are interested to leave the bridge here, we have to disconnect
            # it from the device so that the configurator will allow its
            # removal.
            _disconnect_bridge_port(net_ent_to_remove.name)
    else:
        net_ent_to_remove = net_ent

    # We must first remove the libvirt network and then the network entity.
    # Otherwise if we first remove the network entity while the libvirt
    # network is still up, the network entity (In some flows) thinks that
    # it still has users and thus does not allow its removal
    configurator.removeLibvirtNetwork(network)
    if net_ent_to_remove is not None:
        logging.info('Removing network entity %s', net_ent_to_remove)
        net_ent_to_remove.remove()
    # We must remove the QoS last so that no devices nor networks mark the
    # QoS as used
    backing_device = hierarchy_backing_device(net_ent)
    if (backing_device is not None and
            os.path.exists(netinfo.NET_PATH + '/' + backing_device.name)):
        configurator.removeQoS(net_ent)


def clientSeen(timeout):
    start = time.time()
    while timeout >= 0:
        try:
            if os.stat(constants.P_VDSM_CLIENT_LOG).st_mtime > start:
                return True
        except OSError as e:
            if e.errno == errno.ENOENT:
                pass  # P_VDSM_CLIENT_LOG is not yet there
            else:
                raise
        time.sleep(1)
        timeout -= 1
    return False


def editNetwork(oldBridge, newBridge, vlan=None, bonding=None, nics=None,
                **options):
    with ConfiguratorClass() as configurator:
        _delNetwork(oldBridge, configurator=configurator, **options)
        _addNetwork(newBridge, vlan=vlan, bonding=bonding, nics=nics,
                    configurator=configurator, **options)
        if utils.tobool(options.get('connectivityCheck', False)):
            if not clientSeen(_get_connectivity_timeout(options)):
                _delNetwork(newBridge, force=True)
                raise ConfigNetworkError(ne.ERR_LOST_CONNECTION,
                                         'connectivity check failed')


def _wait_for_udev_events():
    # FIXME: This is an ugly hack that is meant to prevent VDSM to report VFs
    # that are not yet named by udev or not report all of. This is a blocking
    # call that should wait for all udev events to be handled. a proper fix
    # should be registering and listening to the proper netlink and udev
    # events. The sleep prior to observing udev is meant to decrease the
    # chances that we wait for udev before it knows from the kernel about the
    # new devices.
    time.sleep(0.5)
    udevadm.settle(timeout=10)


def _update_numvfs(device_name, numvfs):
    with open(_SYSFS_SRIOV_NUMVFS.format(device_name), 'w', 0) as f:
        # Zero needs to be written first in order to remove previous VFs.
        # Trying to just write the number (if n > 0 VF's existed before)
        # results in 'write error: Device or resource busy'
        # https://www.kernel.org/doc/Documentation/PCI/pci-iov-howto.txt
        f.write('0')
        f.write(str(numvfs))
        _wait_for_udev_events()


def _persist_numvfs(device_name, numvfs):
    dir_path = os.path.join(netconfpersistence.CONF_RUN_DIR,
                            'virtual_functions')
    try:
        os.makedirs(dir_path)
    except OSError as ose:
        if errno.EEXIST != ose.errno:
            raise
    with open(os.path.join(dir_path, device_name), 'w') as f:
        f.write(str(numvfs))


def change_numvfs(device_name, numvfs):
    """Change number of virtual functions of a device.
    The persistence is stored in the same place as other network persistence is
    stored. A call to setSafeNetworkConfig() will persist it across reboots.
    """
    logging.info("changing number of vfs on device %s -> %s.",
                 device_name, numvfs)
    _update_numvfs(device_name, numvfs)

    logging.info("changing number of vfs on device %s -> %s. succeeded.",
                 device_name, numvfs)
    _persist_numvfs(device_name, numvfs)


def _validateNetworkSetup(networks, bondings):
    for network, networkAttrs in networks.iteritems():
        if networkAttrs.get('remove', False):
            net_attr_keys = set(networkAttrs)
            if 'remove' in net_attr_keys and net_attr_keys - set(
                    ['remove', 'custom']):
                raise ConfigNetworkError(
                    ne.ERR_BAD_PARAMS,
                    "Cannot specify any attribute when removing (other "
                    "than custom properties). specified attributes: %s" % (
                        networkAttrs,))

    currentNicsSet = set(netinfo.nics())
    for bonding, bondingAttrs in bondings.iteritems():
        Bond.validateName(bonding)
        if 'options' in bondingAttrs:
            Bond.validateOptions(bondingAttrs['options'])

        if bondingAttrs.get('remove', False):
            continue

        nics = bondingAttrs.get('nics', None)
        if not nics:
            raise ConfigNetworkError(ne.ERR_BAD_PARAMS,
                                     "Must specify nics for bonding")
        if not set(nics).issubset(currentNicsSet):
            raise ConfigNetworkError(ne.ERR_BAD_NIC,
                                     "Unknown nics in: %r" % list(nics))


def _inherit_dhcp_unique_identifier(bridge, _netinfo):
    """
    If there is dhclient already running on a bridge's port we have to use the
    same DHCP unique identifier (DUID) in order to get the same IP address.
    """
    for devices in (_netinfo.nics, _netinfo.bondings, _netinfo.vlans):
        port = devices.get(bridge.port.name)
        if port and port['dhcpv4']:
            bridge.duid_source = bridge.port.name
            break


def _handleBondings(bondings, configurator, in_rollback):
    """ Add/Edit/Remove bond interface """
    logger = logging.getLogger("_handleBondings")

    _netinfo = netinfo.NetInfo()

    edition = []
    addition = []
    for name, attrs in bondings.items():
        if 'remove' in attrs:
            if name not in _netinfo.bondings:
                if in_rollback:
                    logger.error(
                        'Cannot remove bonding %s during rollback: '
                        'does not exist', name)
                    continue
                else:
                    raise ConfigNetworkError(
                        ne.ERR_BAD_BONDING,
                        "Cannot remove bonding %s: does not exist" % name)
            bond = Bond.objectivize(name, configurator, attrs.get('options'),
                                    attrs.get('nics'), mtu=None,
                                    _netinfo=_netinfo,
                                    destroyOnMasterRemoval='remove' in attrs)
            bond.remove()
            del _netinfo.bondings[name]
        elif name in _netinfo.bondings:
            edition.append((name, attrs))
        else:
            addition.append((name, attrs))

    for name, attrs in edition:
        bond = Bond.objectivize(name, configurator, attrs.get('options'),
                                attrs.get('nics'), mtu=None, _netinfo=_netinfo,
                                destroyOnMasterRemoval='remove' in attrs)
        if len(bond.slaves) == 0:
            raise ConfigNetworkError(ne.ERR_BAD_PARAMS, 'Missing required nics'
                                     ' for bonding device.')
        logger.debug("Editing bond %r with options %s", bond, bond.options)
        configurator.editBonding(bond, _netinfo)
    for name, attrs in addition:
        bond = Bond.objectivize(name, configurator, attrs.get('options'),
                                attrs.get('nics'), mtu=None, _netinfo=_netinfo,
                                destroyOnMasterRemoval='remove' in attrs)
        if len(bond.slaves) == 0:
            raise ConfigNetworkError(ne.ERR_BAD_PARAMS, 'Missing required nics'
                                     ' for bonding device.')
        logger.debug("Creating bond %r with options %s", bond, bond.options)
        configurator.configureBond(bond)


def _buildBondOptions(bondName, bondings, _netinfo):
    logger = logging.getLogger("_buildBondOptions")

    bond = {}
    if bondings.get(bondName):
        bond['nics'] = bondings[bondName]['nics']
        bond['bondingOptions'] = bondings[bondName].get('options', None)
    elif bondName in _netinfo.bondings:
        # We may not receive any information about the bonding device if it is
        # unchanged. In this case check whether this bond exists on host and
        # take its parameters.
        logger.debug("Fetching bond %r info", bondName)
        existingBond = _netinfo.bondings[bondName]
        bond['nics'] = existingBond['slaves']
        bond['bondingOptions'] = existingBond['cfg'].get('BONDING_OPTS', None)
    else:
        raise ConfigNetworkError(ne.ERR_BAD_PARAMS, "No bonding option given, "
                                 "nor existing bond %s found." % bondName)
    return bond


def _buildSetupHookDict(req_networks, req_bondings, req_options):

    hook_dict = {'request': {'networks': dict(req_networks),
                             'bondings': dict(req_bondings),
                             'options': dict(req_options)}}

    return hook_dict


def _emergencyNetworkCleanup(network, networkAttrs, configurator):
    """Remove all leftovers after failed setupNetwork"""
    _netinfo = netinfo.NetInfo()

    topNetDev = None
    if 'bonding' in networkAttrs:
        if networkAttrs['bonding'] in _netinfo.bondings:
            topNetDev = Bond.objectivize(networkAttrs['bonding'], configurator,
                                         None, None, None, _netinfo, True)
    elif 'nic' in networkAttrs:
        if networkAttrs['nic'] in _netinfo.nics:
            topNetDev = Nic(networkAttrs['nic'], configurator,
                            _netinfo=_netinfo)
    if 'vlan' in networkAttrs and topNetDev:
        vlan_name = '%s.%s' % (topNetDev.name, networkAttrs['vlan'])
        if vlan_name in _netinfo.vlans:
            topNetDev = Vlan(topNetDev, networkAttrs['vlan'], configurator)
    if networkAttrs['bridged']:
        if network in _netinfo.bridges:
            topNetDev = Bridge(network, configurator, port=topNetDev)

    if topNetDev:
        topNetDev.remove()


def _add_missing_networks(configurator, networks, bondings, force, logger,
                          _netinfo=None):
    # We need to use the newest host info
    if _netinfo is None:
        _netinfo = netinfo.NetInfo()
    else:
        _netinfo.updateDevices()

    for network, attrs in networks.iteritems():
        if 'remove' in attrs:
            continue

        d = dict(attrs)
        if 'bonding' in d:
            d.update(_buildBondOptions(d['bonding'], bondings, _netinfo))
        else:
            d['nics'] = [d.pop('nic')] if 'nic' in d else []
        d['force'] = force

        logger.debug("Adding network %r", network)
        try:
            _addNetwork(network, configurator=configurator,
                        implicitBonding=True, _netinfo=_netinfo, **d)
        except ConfigNetworkError as cne:
            if cne.errCode == ne.ERR_FAILED_IFUP:
                logger.debug("Adding network %r failed. Running "
                             "orphan-devices cleanup", network)
                _emergencyNetworkCleanup(network, attrs,
                                         configurator)
            raise

        _netinfo.updateDevices()  # Things like a bond mtu can change


def _get_connectivity_timeout(options):
    return int(options.get('connectivityTimeout',
                           CONNECTIVITY_TIMEOUT_DEFAULT))


def _check_connectivity(connectivity_check_networks, networks, bondings,
                        options, logger):
    if utils.tobool(options.get('connectivityCheck', True)):
        logger.debug('Checking connectivity...')
        if not clientSeen(_get_connectivity_timeout(options)):
            logger.info('Connectivity check failed, rolling back')
            for network in connectivity_check_networks:
                # If the new added network was created on top of
                # existing bond, we need to keep the bond on rollback
                # flow, else we will break the new created bond.
                _delNetwork(network, force=True,
                            implicitBonding=networks[network].
                            get('bonding') in bondings)
            raise ConfigNetworkError(ne.ERR_LOST_CONNECTION,
                                     'connectivity check failed')


def _apply_hook(bondings, networks, options):
    results = hooks.before_network_setup(_buildSetupHookDict(networks,
                                                             bondings,
                                                             options))
    # gather any changes that could have been done by the hook scripts
    networks = results['request']['networks']
    bondings = results['request']['bondings']
    options = results['request']['options']
    return bondings, networks, options


def _should_keep_bridge(network_attrs, currently_bridged, net_kernel_config):
    marked_for_removal = 'remove' in network_attrs
    if marked_for_removal:
        return False

    should_be_bridged = network_attrs.get('bridged')
    if currently_bridged and not should_be_bridged:
        return False

    # check if the user wanted to only reconfigure bridge itself (mtu is a
    # special case as it is handled automatically by the os)
    def _bridge_only_config(conf):
        return dict(
            (k, v) for k, v in conf.iteritems()
            if k not in ('bonding', 'nic', 'mtu', 'vlan'))

    def _bridge_reconfigured(current_net_conf, required_net_conf):
        return (_bridge_only_config(current_net_conf) !=
                _bridge_only_config(required_net_conf))

    if currently_bridged and _bridge_reconfigured(net_kernel_config,
                                                  network_attrs):
        logging.debug("the bridge is being reconfigured")
        return False

    return True


def setupNetworks(networks, bondings, **options):
    """Add/Edit/Remove configuration for networks and bondings.

    Params:
        networks - dict of key=network, value=attributes
            where 'attributes' is a dict with the following optional items:
                        vlan=<id>
                        bonding="<name>" | nic="<name>"
                        (bonding and nics are mutually exclusive)
                        ipaddr="<ipv4>"
                        netmask="<ipv4>"
                        gateway="<ipv4>"
                        bootproto="..."
                        ipv6addr="<ipv6>[/<prefixlen>]"
                        ipv6gateway="<ipv6>"
                        ipv6autoconf="0|1"
                        dhcpv6="0|1"
                        defaultRoute=True|False
                        (other options will be passed to the config file AS-IS)
                        -- OR --
                        remove=True (other attributes can't be specified)

        bondings - dict of key=bonding, value=attributes
            where 'attributes' is a dict with the following optional items:
                        nics=["<nic1>" , "<nic2>", ...]
                        options="<bonding-options>"
                        -- OR --
                        remove=True (other attributes can't be specified)

        options - dict of options, such as:
                        force=0|1
                        connectivityCheck=0|1
                        connectivityTimeout=<int>
                        _inRollback=True|False

    Notes:
        When you edit a network that is attached to a bonding, it's not
        necessary to re-specify the bonding (you need only to note
        the attachment in the network's attributes). Similarly, if you edit
        a bonding, it's not necessary to specify its networks.
    """
    logger = logging.getLogger("setupNetworks")

    logger.debug("Setting up network according to configuration: "
                 "networks:%r, bondings:%r, options:%r" % (networks,
                                                           bondings, options))

    force = options.get('force', False)
    if not utils.tobool(force):
        logging.debug("Validating configuration")
        _validateNetworkSetup(networks, bondings)

    bondings, networks, options = _apply_hook(bondings, networks, options)

    libvirt_nets = netinfo.networks()
    _netinfo = netinfo.NetInfo(_netinfo=netinfo.get(
        netinfo.libvirtNets2vdsm(libvirt_nets)))
    connectivity_check_networks = set()

    logger.debug("Applying...")
    in_rollback = options.get('_inRollback', False)
    kernel_config = netconfpersistence.KernelConfig(_netinfo)
    normalized_config = kernel_config.normalize(
        netconfpersistence.BaseConfig(networks, bondings))
    with ConfiguratorClass(in_rollback) as configurator:
        # Remove edited networks and networks with 'remove' attribute
        for network, attrs in networks.items():
            if network in _netinfo.networks:
                logger.debug("Removing network %r", network)
                keep_bridge = _should_keep_bridge(
                    network_attrs=normalized_config.networks[network],
                    currently_bridged=_netinfo.networks[network]['bridged'],
                    net_kernel_config=kernel_config.networks[network]
                )

                _delNetwork(network, configurator=configurator, force=force,
                            implicitBonding=False, _netinfo=_netinfo,
                            keep_bridge=keep_bridge)
                del _netinfo.networks[network]
                _netinfo.updateDevices()
            elif network in libvirt_nets:
                # If the network was not in _netinfo but is in the networks
                # returned by libvirt, it means that we are dealing with
                # a broken network.
                logger.debug('Removing broken network %r', network)
                _delBrokenNetwork(network, libvirt_nets[network],
                                  configurator=configurator)
                _netinfo.updateDevices()
            elif 'remove' in attrs:
                raise ConfigNetworkError(ne.ERR_BAD_BRIDGE, "Cannot delete "
                                         "network %r: It doesn't exist in the "
                                         "system" % network)
            else:
                connectivity_check_networks.add(network)

        _handleBondings(bondings, configurator, in_rollback)

        _add_missing_networks(configurator, networks, bondings, force,
                              logger, _netinfo)

        _check_connectivity(connectivity_check_networks, networks, bondings,
                            options, logger)

    hooks.after_network_setup(_buildSetupHookDict(networks, bondings, options))


def _vlanToInternalRepresentation(vlan):
    if vlan is None or vlan == '':
        vlan = None
    else:
        Vlan.validateTag(vlan)
        vlan = int(vlan)
    return vlan


def setSafeNetworkConfig():
    """Declare current network configuration as 'safe'"""
    utils.execCmd([constants.EXT_VDSM_STORE_NET_CONFIG,
                  config.get('vars', 'net_persistence')])


def usage():
    print("""Usage:
    ./api.py add Network <attributes> <options>
             edit oldNetwork newNetwork <attributes> <options>
             del Network <options>
             setup Network [None|attributes] \
[++ Network [None|attributes] [++ ...]] [:: <options>]

                       attributes = [vlan=...] [bonding=...] [nics=<nic1>,...]
                       options = [Force=<True|False>] [bridged=<True|False>]...
    """)


def _parseKwargs(args):
    import API

    kwargs = dict(arg.split('=', 1) for arg in args)
    API.Global.translateNetOptionsToNew(kwargs)

    return kwargs


def main():
    if len(sys.argv) <= 1:
        usage()
        raise ConfigNetworkError(ne.ERR_BAD_PARAMS, "No action specified")
    if sys.argv[1] == 'list':
        listNetworks()
        return
    if len(sys.argv) <= 2:
        usage()
        raise ConfigNetworkError(ne.ERR_BAD_PARAMS, "No action specified")
    if sys.argv[1] == 'add':
        bridge = sys.argv[2]
        kwargs = _parseKwargs(sys.argv[3:])
        if 'nics' in kwargs:
            kwargs['nics'] = kwargs['nics'].split(',')
        # Remove empty vlan and bonding so that they don't make it to
        # _alterRunningConfig
        if 'vlan' in kwargs and kwargs['vlan'] == '':
            del kwargs['vlan']
        if 'bonding' in kwargs and kwargs['bonding'] == '':
            del kwargs['bonding']
        _addNetwork(bridge, **kwargs)
    elif sys.argv[1] == 'del':
        bridge = sys.argv[2]
        kwargs = _parseKwargs(sys.argv[3:])
        if 'nics' in kwargs:
            kwargs['nics'] = kwargs['nics'].split(',')
        _delNetwork(bridge, **kwargs)
    elif sys.argv[1] == 'edit':
        oldBridge = sys.argv[2]
        newBridge = sys.argv[3]
        kwargs = _parseKwargs(sys.argv[4:])
        if 'nics' in kwargs:
            kwargs['nics'] = kwargs['nics'].split(',')
        editNetwork(oldBridge, newBridge, **kwargs)
    elif sys.argv[1] == 'setup':
        batchCommands, options = utils.listSplit(sys.argv[2:], '::', 1)
        d = {}
        for batchCommand in utils.listSplit(batchCommands, '++'):
            d[batchCommand[0]] = _parseKwargs(batchCommand[1:]) or None
        setupNetworks(d, **_parseKwargs(options))
    elif sys.argv[1] == 'show':
        bridge = sys.argv[2]
        kwargs = _parseKwargs(sys.argv[3:])
        showNetwork(bridge, **kwargs)
    else:
        usage()
        raise ConfigNetworkError(ne.ERR_BAD_PARAMS, "Unknown action specified")

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except ConfigNetworkError as e:
        traceback.print_exc()
        print(e.message)
        sys.exit(e.errCode)
    sys.exit(0)
