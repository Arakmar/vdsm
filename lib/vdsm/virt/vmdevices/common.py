#
# Copyright 2008-2017 Red Hat, Inc.
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

from vdsm.config import config
from vdsm.virt import metadata
from vdsm.virt import vmxml

from . import core
from . import graphics
from . import hostdevice
from . import hwclass
from . import lease
from . import network
from . import storage
from . import storagexml


def _update_unknown_device_info(vm):
    """
    Obtain info about unknown devices from libvirt domain and update the
    corresponding device structures.  Unknown device is a device that has an
    address but wasn't passed during VM creation request.

    :param vm: VM for which the device info should be updated
    :type vm: `class:Vm` instance

    """
    def isKnownDevice(alias):
        for dev in vm.conf['devices']:
            if dev.get('alias') == alias:
                return True
        return False

    for x in vmxml.children(vm.domain.devices):
        # Ignore empty nodes and devices without address
        if vmxml.find_first(x, 'address', None) is None:
            continue

        alias = core.find_device_alias(x)
        if not isKnownDevice(alias):
            address = vmxml.device_address(x)
            # In general case we assume that device has attribute 'type',
            # if it hasn't dom_attribute returns ''.
            device = vmxml.attr(x, 'type')
            newDev = {'type': vmxml.tag(x),
                      'alias': alias,
                      'device': device,
                      'address': address}
            vm.conf['devices'].append(newDev)


def update_device_info(vm, devices):
    """
    Obtain info about VM devices from libvirt domain and update the
    corresponding device structures.

    :param vm: VM for which the device info should be updated
    :type vm: `class:Vm` instance
    :param devices: Device configuration of the given VM.
    :type devices: dict

    """
    network.Interface.update_device_info(vm, devices[hwclass.NIC])
    storage.Drive.update_device_info(vm, devices[hwclass.DISK])
    core.Sound.update_device_info(vm, devices[hwclass.SOUND])
    graphics.Graphics.update_device_info(vm, devices[hwclass.GRAPHICS])
    core.Video.update_device_info(vm, devices[hwclass.VIDEO])
    core.Controller.update_device_info(vm, devices[hwclass.CONTROLLER])
    core.Balloon.update_device_info(vm, devices[hwclass.BALLOON])
    core.Watchdog.update_device_info(vm, devices[hwclass.WATCHDOG])
    core.Smartcard.update_device_info(vm, devices[hwclass.SMARTCARD])
    core.Rng.update_device_info(vm, devices[hwclass.RNG])
    core.Console.update_device_info(vm, devices[hwclass.CONSOLE])
    hostdevice.HostDevice.update_device_info(vm, devices[hwclass.HOSTDEV])
    core.Memory.update_device_info(vm, devices[hwclass.MEMORY])
    lease.Device.update_device_info(vm, devices[hwclass.LEASE])
    # Obtain info of all unknown devices. Must be last!
    _update_unknown_device_info(vm)


def lookup_device_by_alias(devices, dev_type, alias):
    for dev in devices[dev_type][:]:
        try:
            if dev.alias == alias:
                return dev
        except AttributeError:
            continue
    raise LookupError('Device instance for device identified by alias %s '
                      'and type %s not found' % (alias, dev_type,))


def lookup_conf_by_alias(conf, dev_type, alias):
    for dev_conf in conf[:]:
        try:
            if dev_conf['alias'] == alias and dev_conf['type'] == dev_type:
                return dev_conf
        except KeyError:
            continue
    raise LookupError('Configuration of device identified by alias %s '
                      'and type %s not found' % (alias, dev_type,))


_DEVICE_MAPPING = {
    hwclass.DISK: storage.Drive,
    hwclass.NIC: network.Interface,
    hwclass.SOUND: core.Sound,
    hwclass.VIDEO: core.Video,
    hwclass.GRAPHICS: graphics.Graphics,
    hwclass.CONTROLLER: core.Controller,
    hwclass.GENERAL: core.Generic,
    hwclass.BALLOON: core.Balloon,
    hwclass.WATCHDOG: core.Watchdog,
    hwclass.CONSOLE: core.Console,
    hwclass.REDIR: core.Redir,
    hwclass.RNG: core.Rng,
    hwclass.SMARTCARD: core.Smartcard,
    hwclass.TPM: core.Tpm,
    hwclass.HOSTDEV: hostdevice.HostDevice,
    hwclass.MEMORY: core.Memory,
    hwclass.LEASE: lease.Device,
}


_LIBVIRT_TO_OVIRT_NAME = {
    'memballoon': hwclass.BALLOON,
}


def identify_from_xml_elem(dev_elem):
    dev_type = dev_elem.tag
    dev_name = _LIBVIRT_TO_OVIRT_NAME.get(dev_type, dev_type)
    if dev_name not in _DEVICE_MAPPING:
        raise core.SkipDevice()
    return dev_name, _DEVICE_MAPPING[dev_name]


def empty_dev_map():
    return {dev: [] for dev in _DEVICE_MAPPING}


def dev_map_from_dev_spec_map(dev_spec_map, log):
    dev_map = empty_dev_map()

    for dev_type, dev_class in _DEVICE_MAPPING.items():
        for dev in dev_spec_map[dev_type]:
            dev_map[dev_type].append(dev_class(log, **dev))

    return dev_map


# metadata used by the devices. Unless otherwise specified, type and meaning
# are the same as specified in vdsm-api.yml
#
# * graphics.Graphics:
#    = match by: none, implicit matching. Only one SPICE device is allowed
#                and the VNC device ignores the metadata
#    = keys:
#      - display_network
#
#    = example:
#      <metadata xmlns:ovirt-vm='http://ovirt.org/vm/1.0'>
#        <ovirt-vm:vm>
#          <ovirt-vm:device type='graphics'>
#            <ovirt-vm:display_network>ovirtmgmt</ovirt-vm:display_network>
#          </ovirt-vm:device>
#        </ovirt-vm:vm>
#      </metadata>
#
# * network.Interface:
#    = match by: 'mac_address'
#
#    = keys:
#      - network
#
#    = example:
#      <metadata xmlns:ovirt-vm='http://ovirt.org/vm/1.0'>
#        <ovirt-vm:vm>
#          <ovirt-vm:device type='interface' mac_address='...'>
#            <ovirt-vm:network>ovirtmgmt</ovirt-vm:network>
#          </ovirt-vm:device>
#        </ovirt-vm:vm>
#      </metadata>
def dev_map_from_domain_xml(vmid, dom_desc, md_desc, log):
    """
    Create a device map - same format as empty_dev_map from a domain XML
    representation. The domain XML is accessed through a Domain Descriptor.

    :param vmid: UUID of the vm whose devices need to be initialized.
    :type vmid: basestring
    :param dom_desc: domain descriptor to provide access to the domain XML
    :type dom_desc: `class DomainDescriptor`
    :param md_desc: metadata descriptor to provide access to the device
                    metadata
    :type md_desc: `class metadata.Descriptor`
    :param log: logger instance to use for messages, and to pass to device
    objects.
    :type log: logger instance, as returned by logging.getLogger()
    :return: map of initialized devices, map of devices needing refresh.
    :rtype: A device map, in the same format as empty_dev_map() would return.
    """

    log.debug('Initializing device classes from domain XML')
    dev_map = empty_dev_map()
    for dev_type, dev_class, dev_elem in _device_elements(dom_desc, log):
        dev_meta = _get_metadata_from_elem_xml(vmid, md_desc,
                                               dev_class, dev_elem)
        try:
            dev_obj = dev_class.from_xml_tree(log, dev_elem, dev_meta)
        except NotImplementedError:
            log.debug('Cannot initialize %s device: not implemented',
                      dev_type)
        else:
            dev_map[dev_type].append(dev_obj)
    log.debug('Initialized %d device classes from domain XML', len(dev_map))
    return dev_map


def dev_elems_from_xml(vm, xml):
    """
    Return device instance building elements from provided XML.

    The XML must contain <devices> element with a single device subelement, the
    one to create the instance for.  Depending on the device kind <metadata>
    element may be required to provide device metadata; the element may and
    needn't contain unrelated metadata.  This function is used in device
    hot(un)plugs.

    Example `xml` value (top element tag may be arbitrary):

      <?xml version='1.0' encoding='UTF-8'?>
      <hotplug>
        <devices>
          <interface type="bridge">
            <mac address="66:55:44:33:22:11"/>
            <model type="virtio" />
            <source bridge="ovirtmgmt" />
            <filterref filter="vdsm-no-mac-spoofing" />
            <link state="up" />
            <bandwidth />
          </interface>
        </devices>
        <metadata xmlns:ns0="http://ovirt.org/vm/tune/1.0"
                  xmlns:ovirt-vm="http://ovirt.org/vm/1.0">
          <ovirt-vm:vm xmlns:ovirt-vm="http://ovirt.org/vm/1.0">
            <ovirt-vm:device mac_address='66:55:44:33:22:11'>
              <ovirt-vm:network>test</ovirt-vm:network>
              <ovirt-vm:portMirroring>
                <ovirt-vm:network>network1</ovirt-vm:network>
                <ovirt-vm:network>network2</ovirt-vm:network>
              </ovirt-vm:portMirroring>
            </ovirt-vm:device>
          </ovirt-vm:vm>
        </metadata>
      </hotplug>

    :param xml: XML specifying the device as described above.
    :type xml: basestring
    :returns: Triplet (device_class, device_element, device_meta) where
      `device_class` is the class to be used to create the device instance;
      `device_element` and `device_meta` are objects to be passed as arguments
      to device_class `from_xml_tree` method.
    """
    dom = vmxml.parse_xml(xml)
    devices = vmxml.find_first(dom, 'devices')
    dev_elem = next(vmxml.children(devices))
    _dev_type, dev_class = identify_from_xml_elem(dev_elem)
    meta = vmxml.find_first(dom, 'metadata', None)
    if meta is None:
        md_desc = metadata.Descriptor()
    else:
        md_desc = metadata.Descriptor.from_xml(vmxml.format_xml(meta))
    dev_meta = _get_metadata_from_elem_xml(vm.id, md_desc, dev_class, dev_elem)
    return dev_class, dev_elem, dev_meta


def dev_from_xml(vm, xml):
    """
    Create and return device instance from provided XML.

    `dev_elems_from_xml` is called to extract device building elements and then
    the device instance is created from it and returned.

    :param xml: XML specifying the device as described in `dev_elems_from_xml`.
    :type xml: basestring
    :returns: Device instance created from the provided XML.
    """
    cls, elem, meta = dev_elems_from_xml(vm, xml)
    return cls.from_xml_tree(vm.log, elem, meta)


def storage_device_params_from_domain_xml(vmid, dom_desc, md_desc, log):
    log.debug('Extracting storage devices params from domain XML')
    params = []
    for dev_type, dev_class, dev_elem in _device_elements(dom_desc, log):
        if dev_type != hwclass.DISK:
            log.debug('skipping non-storage device: %r', dev_elem.tag)
            continue

        dev_meta = _get_metadata_from_elem_xml(vmid, md_desc,
                                               dev_class, dev_elem)
        params.append(storagexml.parse(dev_elem, dev_meta))
    log.debug('Extracted %d storage devices params from domain XML',
              len(params))
    return params


def get_metadata(dev_class, dev_obj):
    # storage devices are special, and they need separate treatment
    if dev_class != hwclass.DISK:
        return dev_obj.get_metadata(dev_class)
    return storagexml.get_metadata(dev_obj)


def save_device_metadata(md_desc, dev_map, log):
    log.debug('Saving the device metadata into domain XML')
    count = 0
    for dev_class, dev_objs in dev_map.items():
        for dev_obj in dev_objs:
            attrs, data = get_metadata(dev_class, dev_obj)
            if not data:
                # the device doesn't want to save anything.
                # let's go ahead.
                continue
            elif not attrs:
                # data with no attrs? most likely a bug.
                log.warning('No metadata attrs for %s', dev_obj)
                continue

            with md_desc.device(**attrs) as dev:
                dev.clear()
                dev.update(data)
                count += 1

    log.debug('Saved %d device metadata', count)


def get_refreshable_device_classes():
    config_value = config.get('devel', 'device_xml_refresh_enable').strip()
    if config_value == 'ALL':
        return set(hwclass.TO_REFRESH)

    refresh_whitelist = set(
        dev_class.strip().lower()
        for dev_class in config_value.split(',')
    )
    return set(
        dev_class for dev_class in hwclass.TO_REFRESH
        if dev_class in refresh_whitelist
    )


def replace_devices_xml(domxml, devices_xml):
    devices = vmxml.find_first(domxml, 'devices', None)

    refreshable = get_refreshable_device_classes()

    old_devs = [
        dev for dev in vmxml.children(devices)
        if dev.tag in refreshable
    ]
    for old_dev in old_devs:
        vmxml.remove_child(devices, old_dev)

    for dev_class in refreshable:
        for dev in devices_xml[dev_class]:
            vmxml.append_child(devices, etree_child=dev)

    return domxml


def _device_elements(dom_desc, log):
    for dev_elem in vmxml.children(dom_desc.devices):
        try:
            dev_type, dev_class = identify_from_xml_elem(dev_elem)
        except core.SkipDevice:
            log.debug('skipping unhandled device: %r', dev_elem.tag)
        else:
            yield dev_type, dev_class, dev_elem


def _get_metadata_from_elem_xml(vmid, md_desc, dev_class, dev_elem):
    dev_meta = {'vmid': vmid}
    attrs = dev_class.get_identifying_attrs(dev_elem)
    if attrs:
        with md_desc.device(**attrs) as dev_data:
            dev_meta.update(dev_data)
    return dev_meta


def update_guest_disk_mapping(md_desc, disk_devices, guest_disk_mapping, log):
    for serial, value in guest_disk_mapping:
        for d in disk_devices:
            image_id = storage.image_id(d.path)
            if image_id and image_id[:20] in serial:
                d.guestName = value['name']
                log.debug("Guest name of drive %s: %s",
                          image_id, d.guestName)
                attrs, data = storagexml.get_metadata(d)
                with md_desc.device(**attrs) as dev:
                    dev.update(data)
                break
        else:
            if serial[20:]:
                # Silently skip devices that don't appear to have a serial
                # number, such as CD-ROMs or direct LUN devices.
                log.warning("Unidentified guest drive %s: %s",
                            serial, value['name'])
