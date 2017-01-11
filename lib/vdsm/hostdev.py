#
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
from __future__ import absolute_import

import collections
import functools
import os
import xml.etree.cElementTree as etree

import libvirt

from . import cpuarch
from . import hooks
from . import libvirtconnection
from . import supervdsm
from . import utils

CAPABILITY_TO_XML_ATTR = {'pci': 'pci',
                          'scsi': 'scsi',
                          'scsi_generic': 'scsi_generic',
                          'usb_device': 'usb'}

_LIBVIRT_DEVICE_FLAGS = {
    'system': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_SYSTEM,
    'pci': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_PCI_DEV,
    'usb_device': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_USB_DEV,
    'usb': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_USB_INTERFACE,
    'net': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_NET,
    'scsi_host': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_SCSI_HOST,
    'scsi_target': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_SCSI_TARGET,
    'scsi': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_SCSI,
    'storage': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_STORAGE,
    'fc_host': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_FC_HOST,
    'vports': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_VPORTS,
    'scsi_generic': libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_SCSI_GENERIC,
}

_DATA_PROCESSORS = collections.defaultdict(list)


class PCIHeaderType:
    ENDPOINT = 0
    BRIDGE = 1
    CARDBUS_BRIDGE = 2
    UNKNOWN = 99


class NoIOMMUSupportException(Exception):
    pass


class UnsuitableSCSIDevice(Exception):
    pass


class _DeviceTreeCache(object):

    def __init__(self, devices):
        self._parent_to_device_name = {}
        # Store a reference so we can look up the params
        self.devices = devices
        self._populate(devices)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._invalidate()

    def get_by_parent(self, capability, parent_name):
        try:
            return self.devices[
                self._parent_to_device_params[capability][parent_name]]
        except KeyError:
            return None

    def _populate(self, devices):
        self._parent_to_device_params = collections.defaultdict(dict)

        for device_name, device_params in devices.items():
            try:
                parent = device_params['parent']
            except KeyError:
                continue

            self._parent_to_device_params[
                device_params['capability']][parent] = device_name

    def _invalidate(self):
        self._parent_to_device_params = {}


@utils.memoized
def _data_processors_map():
    data_processors_map = {}
    for capability in _LIBVIRT_DEVICE_FLAGS:
        data_processors_map[capability] = (_DATA_PROCESSORS['_ANY'] +
                                           _DATA_PROCESSORS[capability])
    return data_processors_map


def _data_processor(target_bus='_ANY'):
    """
    Register function as a data processor for device processing code.
    """
    def processor(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            return function(*args, **kwargs)
        _DATA_PROCESSORS[target_bus].append(wrapped)
        return wrapped
    return processor


def is_supported():
    try:
        iommu_groups_exist = bool(len(os.listdir('/sys/kernel/iommu_groups')))
        if cpuarch.is_ppc(cpuarch.real()):
            return iommu_groups_exist

        dmar_exists = bool(len(os.listdir('/sys/class/iommu')))
        return iommu_groups_exist and dmar_exists
    except OSError:
        return False


def _pci_header_type(device_name):
    """
    PCI header type is 1 byte located at 0x0e of PCI configuration space.
    Relevant part of the header structures:

    register (offset)|bits 31-24|bits 23-16 |bits 15-8    |bits 7-0
    0C               |BIST      |Header type|Latency Timer|Cache Line Size

    The structure of type looks like this:

    Bit 7             |Bits 6 to 0
    Multifunction flag|Header Type

    This function should be replaced when [1] is resolved.

    [1]https://bugzilla.redhat.com/show_bug.cgi?id=1317531
    """
    try:
        with open('/sys/bus/pci/devices/{}/config'.format(
                name_to_pci_path(device_name)), 'rb') as f:
            f.seek(0x0e)
            header_type = ord(f.read(1)) & 0x7f
    except IOError:
        return PCIHeaderType.UNKNOWN

    return int(header_type)


def name_to_pci_path(device_name):
    return device_name[4:].replace('_', '.').replace('.', ':', 2)


def scsi_address_to_adapter(scsi_address):
    """
    Read adapter info from scsi host address, and mutate the adress (removing
    'host' key) to conform to libvirt.
    """
    adapter = 'scsi_host{}'.format(scsi_address['host'])
    scsi_address['unit'] = scsi_address['lun']
    del scsi_address['lun']
    del scsi_address['host']

    return {'name': adapter}


def pci_address_to_name(domain, bus, slot, function):
    """
    Convert 4 attributes that identify the pci device on the bus to
    libvirt's pci name: pci_${domain}_${bus}_${slot}_${function}.
    The first 2 characters are hex notation that is unwanted in the name.
    """
    return 'pci_{0}_{1}_{2}_{3}'.format(domain[2:],
                                        bus[2:],
                                        slot[2:],
                                        function[2:])


def _sriov_totalvfs(device_name):
    with open('/sys/bus/pci/devices/{0}/sriov_totalvfs'.format(
            name_to_pci_path(device_name))) as f:
        return int(f.read())


def physical_function_net_name(pf_pci_name):
    """
    takes a pci path of a physical function (e.g. pci_0000_02_00_0) and returns
    the network interface name associated with it (e.g. enp2s0f0)
    """
    devices = list_by_caps()
    libvirt_device_names = [name for name, device in devices.iteritems()
                            if device['params'].get('parent') == pf_pci_name]
    if len(libvirt_device_names) > 1:
        raise Exception('could not determine network name for %s. Possible'
                        'devices: %s' % (pf_pci_name, libvirt_device_names))
    if not libvirt_device_names:
        raise Exception('could not determine network name for %s. There are no'
                        'devices with this parent.' % (pf_pci_name,))

    return libvirt_device_names[0].split('_')[1]


def _process_address(device_xml, children):
    params = {}
    for cap in children:
        params[cap] = device_xml.find('./capability/{}'.format(cap)).text

    return {'address': params}


@_data_processor('pci')
def _process_pci_address(device_xml):
    return _process_address(device_xml, ('domain', 'bus', 'slot', 'function'))


@_data_processor('scsi')
def _process_scsi_address(device_xml):
    return _process_address(device_xml, ('host', 'bus', 'target', 'lun'))


@_data_processor('usb_device')
def _process_usb_address(device_xml):
    return _process_address(device_xml, ('bus', 'device'))


@_data_processor('pci')
def _process_assignability(device_xml):
    is_assignable = None

    physfn = device_xml.find('./capability/capability')

    if physfn is not None:
        if physfn.attrib['type'] in ('pci-bridge', 'cardbus-bridge'):
            is_assignable = 'false'
    if is_assignable is None:
        name = device_xml.find('name').text
        is_assignable = str(_pci_header_type(name) ==
                            PCIHeaderType.ENDPOINT).lower()

    return {'is_assignable': is_assignable}


@_data_processor('scsi_generic')
def _process_udev_path(device_xml):
    try:
        udev_path = device_xml.find('./capability/char').text
    except AttributeError:
        return {}
    else:
        return {'udev_path': udev_path}


@_data_processor()
def _process_driver(device_xml):
    try:
        driver_name = device_xml.find('./driver/name').text
    except AttributeError:
        # No driver exposed by libvirt/sysfs.
        return {}
    else:
        return {'driver': driver_name}


@_data_processor('storage')
def _process_storage(device_xml):
    try:
        model = device_xml.find('./capability/model').text
    except AttributeError:
        return {}
    else:
        return {'product': model}


@_data_processor('pci')
def _process_vfs(device_xml):
    name = device_xml.find('name').text

    try:
        return {'totalvfs': _sriov_totalvfs(name)}
    except IOError:
        # Device does not support sriov, we can safely go on
        return {}


@_data_processor('pci')
def _process_iommu(device_xml):
    iommu_group = device_xml.find('./capability/iommuGroup')
    if iommu_group is not None:
        return {'iommu_group': iommu_group.attrib['number']}
    return {}


@_data_processor('pci')
def _process_physfn(device_xml):
    physfn = device_xml.find('./capability/capability')
    if physfn is not None and physfn.attrib['type'] == 'phys_function':
        address = physfn.find('address')
        return {'physfn': pci_address_to_name(**address.attrib)}
    return {}


@_data_processor()
def _process_productinfo(device_xml):
    params = {}

    capabilities = device_xml.findall('./capability/')
    for capability in capabilities:
        if capability.tag in ('vendor', 'product', 'interface'):
            if 'id' in capability.attrib:
                params[capability.tag + '_id'] = capability.attrib['id']
            if capability.text:
                params[capability.tag] = capability.text

    return params


@_data_processor()
def _process_parent(device_xml):
    name = device_xml.find('name').text

    if name != 'computer':
        return {'parent': device_xml.find('parent').text}

    return {}


def _process_scsi_device_params(device_name, cache):
    """
    The information we need about SCSI device is contained within multiple
    sysfs devices:

    * vendor and product (not really, more of "human readable name") are
      provided by capability 'storage',
    * path to udev file (/dev/sgX) is provided by 'scsi_generic' capability
      and is required to set correct permissions.

    When reporting the devices via list_by_caps, we don't care if either of
    the devices are not found as the information provided is purely cosmetic.
    If the device is queried in hostdev object creation flow, vendor and
    product are still unnecessary, but udev_path becomes essential.
    """
    params = {}

    storage_dev_params = cache.get_by_parent('storage', device_name)
    if storage_dev_params:
        for attr in ('vendor', 'product'):
            try:
                res = storage_dev_params[attr]
            except KeyError:
                pass
            else:
                params[attr] = res

    scsi_generic_dev_params = cache.get_by_parent('scsi_generic', device_name)
    if scsi_generic_dev_params:
        params['udev_path'] = scsi_generic_dev_params['udev_path']

    return params


def _process_device_params(device_xml):
    """
    Process device_xml and return dict of found known parameters,
    also doing sysfs lookups for sr-iov related information
    """
    params = {}

    devXML = etree.fromstring(device_xml.decode('ascii', errors='ignore'))

    caps = devXML.find('capability')
    params['capability'] = caps.attrib['type']
    params['is_assignable'] = 'true'

    for data_processor in _data_processors_map()[params['capability']]:
        params.update(data_processor(devXML))

    return params


def _get_device_ref_and_params(device_name):
    libvirt_device = libvirtconnection.get().\
        nodeDeviceLookupByName(device_name)
    params = _process_device_params(libvirt_device.XMLDesc(0))

    if params['capability'] != 'scsi':
        return libvirt_device, params

    flags = (_LIBVIRT_DEVICE_FLAGS['storage'] +
             _LIBVIRT_DEVICE_FLAGS['scsi_generic'])
    devices = dict((device.name(), _process_device_params(device.XMLDesc(0)))
                   for device in libvirtconnection.get().listAllDevices(flags))
    with _DeviceTreeCache(devices) as cache:
        params.update(_process_scsi_device_params(device_name, cache))

    return libvirt_device, params


def _get_devices_from_libvirt(flags=0):
    """
    Returns all available host devices from libvirt processd to dict
    """
    devices = dict((device.name(), _process_device_params(device.XMLDesc(0)))
                   for device in libvirtconnection.get().listAllDevices(flags))

    with _DeviceTreeCache(devices) as cache:
        for device_name, device_params in devices.items():
            if device_params['capability'] == 'scsi':
                device_params.update(
                    _process_scsi_device_params(device_name, cache))
    return devices


def list_by_caps(caps=None):
    """
    Returns devices that have specified capability in format
    {device_name: {'params': {'capability': '', 'vendor': '',
                              'vendor_id': '', 'product': '',
                              'product_id': '', 'iommu_group': ''},
                   'vmId': vmId]}

    caps -- list of strings determining devices of which capabilities
            will be returned (e.g. ['pci', 'usb'] -> pci and usb devices)
    """
    devices = {}
    flags = sum([_LIBVIRT_DEVICE_FLAGS[cap] for cap in caps or []])
    libvirt_devices = _get_devices_from_libvirt(flags)

    for devName, params in libvirt_devices.items():
        devices[devName] = {'params': params}

    devices = hooks.after_hostdev_list_by_caps(devices)
    return devices


def get_device_params(device_name):
    _, device_params = _get_device_ref_and_params(device_name)
    return device_params


def detach_detachable(device_name):
    libvirt_device, device_params = _get_device_ref_and_params(device_name)
    capability = CAPABILITY_TO_XML_ATTR[device_params['capability']]

    if capability == 'pci' and utils.tobool(device_params['is_assignable']):
        try:
            iommu_group = device_params['iommu_group']
        except KeyError:
            raise NoIOMMUSupportException('hostdev passthrough without iommu')
        supervdsm.getProxy().appropriateIommuGroup(iommu_group)
        libvirt_device.detachFlags(None)
    elif capability == 'usb':
        supervdsm.getProxy().appropriateUSBDevice(
            device_params['address']['bus'],
            device_params['address']['device'])
    elif capability == 'scsi':
        if 'udev_path' not in device_params:
            raise UnsuitableSCSIDevice

        supervdsm.getProxy().appropriateSCSIDevice(device_name,
                                                   device_params['udev_path'])

    return device_params


def reattach_detachable(device_name):
    libvirt_device, device_params = _get_device_ref_and_params(device_name)
    capability = CAPABILITY_TO_XML_ATTR[device_params['capability']]

    if capability == 'pci' and utils.tobool(device_params['is_assignable']):
        try:
            iommu_group = device_params['iommu_group']
        except KeyError:
            raise NoIOMMUSupportException
        supervdsm.getProxy().rmAppropriateIommuGroup(iommu_group)
        libvirt_device.reAttach()
    elif capability == 'usb':
        supervdsm.getProxy().rmAppropriateUSBDevice(
            device_params['address']['bus'],
            device_params['address']['device'])
    elif capability == 'scsi':
        if 'udev_path' not in device_params:
            raise UnsuitableSCSIDevice

        supervdsm.getProxy().rmAppropriateSCSIDevice(
            device_name, device_params['udev_path'])


def change_numvfs(device_name, numvfs):
    net_name = physical_function_net_name(device_name)
    supervdsm.getProxy().change_numvfs(name_to_pci_path(device_name), numvfs,
                                       net_name)
