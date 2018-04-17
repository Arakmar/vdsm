#
# Copyright 2018 Red Hat, Inc.
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


from vdsm.virt.vmdevices import core
from vdsm.virt import vmxml


def drive_from_element(disk_element, disk_devices):
    # we try serial first for backward compatibility
    # REQUIRED_FOR: vdsm <= 4.2
    serial_elem = vmxml.find_first(disk_element, 'serial', None)
    if serial_elem is not None:
        serial = vmxml.text(serial_elem)
        try:
            return drive_by_serial(disk_devices, serial)
        except LookupError:
            pass  # try again by alias before to give up

    alias = core.find_device_alias(disk_element)
    return device_by_alias(disk_devices, alias)


def device_by_alias(devices, alias):
    for device in devices:
        if getattr(device, 'alias', None) == alias:
            return device
    raise LookupError("No such device: alias=%r" % alias)


def drive_by_serial(disk_devices, serial):
    for device in disk_devices:
        if device.serial == serial:
            return device
    raise LookupError("No such drive: '%s'" % serial)


def drive_by_name(disk_devices, name):
    for device in disk_devices:
        if device.name == name:
            return device
    raise LookupError("No such drive: '%s'" % name)
