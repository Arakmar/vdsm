#
# Copyright 2008-2014 Red Hat, Inc.
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

import logging
import xml.etree.ElementTree as ET
from xml.dom import Node

from vdsm import supervdsm
from vdsm import utils
from vdsm.hostdev import get_device_params, detach_detachable
from vdsm.network import api as net_api

from .core import Base
from . import hwclass


class Interface(Base):
    __slots__ = ('nicModel', 'macAddr', 'network', 'bootOrder', 'address',
                 'linkActive', 'portMirroring', 'filter',
                 'sndbufParam', 'driver', 'name', 'vlanId', 'hostdev',
                 'is_hostdevice')

    def __init__(self, conf, log, **kwargs):
        # pyLint can't tell that the Device.__init__() will
        # set a nicModel attribute, so modify the kwarg list
        # prior to device init.
        for attr, value in kwargs.iteritems():
            if attr == 'nicModel' and value == 'pv':
                kwargs[attr] = 'virtio'
            elif attr == 'network' and value == '':
                kwargs[attr] = net_api.DUMMY_BRIDGE
        super(Interface, self).__init__(conf, log, **kwargs)
        self.sndbufParam = False
        self.is_hostdevice = self.device == hwclass.HOSTDEV
        self.vlanId = self.specParams.get('vlanid')
        self._customize()

    def _customize(self):
        # Customize network device
        self.driver = {}

        vhosts = self._getVHostSettings()
        if vhosts:
            self.driver['name'] = vhosts.get(self.network, False)

        try:
            self.driver['queues'] = self.custom['queues']
        except KeyError:
            pass    # interface queues not specified

        try:
            self.sndbufParam = self.conf['custom']['sndbuf']
        except KeyError:
            pass    # custom_sndbuf not specified

    def _getVHostSettings(self):
        VHOST_MAP = {'true': 'vhost', 'false': 'qemu'}
        vhosts = {}
        vhostProp = self.conf.get('custom', {}).get('vhost', '')

        if vhostProp != '':
            for vhost in vhostProp.split(','):
                try:
                    vbridge, vstatus = vhost.split(':', 1)
                    vhosts[vbridge] = VHOST_MAP[vstatus.lower()]
                except (ValueError, KeyError):
                    self.log.warning("Unknown vhost format: %s", vhost)

        return vhosts

    def getXML(self):
        """
        Create domxml for network interface.

        <interface type="bridge">
            <mac address="aa:bb:dd:dd:aa:bb"/>
            <model type="virtio"/>
            <source bridge="engine"/>
            [<driver name="vhost/qemu" queues="int"/>]
            [<filterref filter='filter name'/>]
            [<tune><sndbuf>0</sndbuf></tune>]
            [<link state='up|down'/>]
            [<bandwidth>
              [<inbound average="int" [burst="int"]  [peak="int"]/>]
              [<outbound average="int" [burst="int"]  [peak="int"]/>]
             </bandwidth>]
        </interface>

        -- or -- a slightly different SR-IOV network interface
        <interface type='hostdev' managed='no'>
          <driver name='vfio'/>
          <source>
           <address type='pci' domain='0x0000' bus='0x00' slot='0x07'
           function='0x0'/>
          </source>
          <mac address='52:54:00:6d:90:02'/>
          <vlan>
           <tag id=100/>
          </vlan>
          <address type='pci' domain='0x0000' bus='0x00' slot='0x07'
          function='0x0'/>
          <boot order='1'/>
         </interface>
        """
        iface = self.createXmlElem('interface', self.device, ['address'])
        iface.appendChildWithArgs('mac', address=self.macAddr)

        if hasattr(self, 'nicModel'):
            iface.appendChildWithArgs('model', type=self.nicModel)

        if self.is_hostdevice:
            # SR-IOV network interface
            iface.setAttrs(managed='no')
            host_address = get_device_params(self.hostdev)['address']
            source = iface.appendChildWithArgs('source')
            source.appendChildWithArgs('address', type='pci', **host_address)

            if self.vlanId is not None:
                vlan = iface.appendChildWithArgs('vlan')
                vlan.appendChildWithArgs('tag', id=str(self.vlanId))
        else:
            ovs_bridge = supervdsm.getProxy().ovs_bridge(self.network)
            if ovs_bridge:
                self._source_ovs_bridge(iface, ovs_bridge)
            else:
                iface.appendChildWithArgs('source', bridge=self.network)

        if hasattr(self, 'filter'):
            iface.appendChildWithArgs('filterref', filter=self.filter)

        if hasattr(self, 'linkActive'):
            iface.appendChildWithArgs('link', state='up'
                                      if utils.tobool(self.linkActive)
                                      else 'down')

        if hasattr(self, 'bootOrder'):
            iface.appendChildWithArgs('boot', order=self.bootOrder)

        if self.driver:
            iface.appendChildWithArgs('driver', **self.driver)
        elif self.is_hostdevice:
            iface.appendChildWithArgs('driver', name='vfio')

        if self.sndbufParam:
            tune = iface.appendChildWithArgs('tune')
            tune.appendChildWithArgs('sndbuf', text=self.sndbufParam)

        if 'inbound' in self.specParams or 'outbound' in self.specParams:
            iface.appendChild(self.paramsToBandwidthXML(self.specParams))

        return iface

    def _source_ovs_bridge(self, iface, ovs_bridge):
        iface.appendChildWithArgs('source', bridge=ovs_bridge)
        iface.appendChildWithArgs('virtualport', type='openvswitch')
        vlan_tag = net_api.net2vlan(self.network)
        if vlan_tag:
            vlan = iface.appendChildWithArgs('vlan')
            vlan.appendChildWithArgs('tag', id=str(vlan_tag))

    def paramsToBandwidthXML(self, specParams, oldBandwidth=None):
        """Returns a valid libvirt xml dom element object."""
        bandwidth = self.createXmlElem('bandwidth', None)
        old = {} if oldBandwidth is None else dict(
            (elem.nodeName, elem) for elem in oldBandwidth.childNodes)
        for key in ('inbound', 'outbound'):
            elem = specParams.get(key)
            if elem is None:  # Use the old setting if present
                if key in old:
                    bandwidth.appendChild(old[key])
            elif elem:
                # Convert the values to string for adding them to the XML def
                attrs = dict((key, str(value)) for key, value in elem.items())
                bandwidth.appendChildWithArgs(key, **attrs)
        return bandwidth

    def setup(self):
        if self.is_hostdevice:
            logging.debug('Detaching device %s from the host.' % self.device)
            detach_detachable(self.hostdev)

    @property
    def _xpath(self):
        """
        Returns xpath to the device in libvirt dom xml
        The path is relative to the root element
        """
        return "./devices/interface/mac[@address='%s']" % self.macAddr

    def is_attached_to(self, xml_string):
        dom = ET.fromstring(xml_string)
        return bool(dom.findall(self._xpath))

    @classmethod
    def update_device_info(cls, vm, device_conf):
        for x in vm.domain.get_device_elements('interface'):
            devType = x.getAttribute('type')
            mac = x.getElementsByTagName('mac')[0].getAttribute('address')
            alias = x.getElementsByTagName('alias')[0].getAttribute('name')
            xdrivers = x.getElementsByTagName('driver')
            driver = ({'name': xdrivers[0].getAttribute('name'),
                       'queues': xdrivers[0].getAttribute('queues')}
                      if xdrivers else {})
            if devType == 'hostdev':
                name = alias
                model = 'passthrough'
            else:
                name = x.getElementsByTagName('target')[0].getAttribute('dev')
                model = x.getElementsByTagName('model')[0].getAttribute('type')

            network = None
            try:
                if x.getElementsByTagName('link')[0].getAttribute('state') == \
                        'down':
                    linkActive = False
                else:
                    linkActive = True
            except IndexError:
                linkActive = True
            source = x.getElementsByTagName('source')
            if source:
                network = source[0].getAttribute('bridge')
                if not network:
                    network = net_api.netname_l2o(
                        source[0].getAttribute('network'))

            # Get nic address
            address = {}
            # TODO: fix vmxml.device_address and its users to have this code.
            for child in x.childNodes:
                if (child.nodeType != Node.TEXT_NODE and
                        child.tagName == 'address'):
                    address = dict((k.strip(), child.getAttribute(k).strip())
                                   for k in child.attributes.keys())
                    break

            for nic in device_conf:
                if nic.macAddr.lower() == mac.lower():
                    nic.name = name
                    nic.alias = alias
                    nic.address = address
                    nic.linkActive = linkActive
                    if driver:
                        # If a driver was reported, pass it back to libvirt.
                        # Engine (vm's conf) is not interested in this value.
                        nic.driver = driver
            # Update vm's conf with address for known nic devices
            knownDev = False
            for dev in vm.conf['devices']:
                if (dev['type'] == hwclass.NIC and
                        dev['macAddr'].lower() == mac.lower()):
                    dev['address'] = address
                    dev['alias'] = alias
                    dev['name'] = name
                    dev['linkActive'] = linkActive
                    knownDev = True
            # Add unknown nic device to vm's conf
            if not knownDev:
                nicDev = {'type': hwclass.NIC,
                          'device': devType,
                          'macAddr': mac,
                          'nicModel': model,
                          'address': address,
                          'alias': alias,
                          'name': name,
                          'linkActive': linkActive}
                if network:
                    nicDev['network'] = network
                vm.conf['devices'].append(nicDev)
