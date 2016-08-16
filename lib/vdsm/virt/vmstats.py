#
# Copyright 2008-2016 Red Hat, Inc.
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

import contextlib
import logging

import six

from vdsm import metrics
from vdsm.utils import convertToStr

from vdsm.utils import monotonic_time
from vdsm.virt.utils import isVdsmImage


_MBPS_TO_BPS = 10 ** 6 / 8


def produce(vm, first_sample, last_sample, interval):
    """
    Translates vm samples into stats.
    """

    stats = {}

    cpu(stats, first_sample, last_sample, interval)
    networks(vm, stats, first_sample, last_sample, interval)
    disks(vm, stats, first_sample, last_sample, interval)
    balloon(vm, stats, last_sample)
    cpu_count(stats, last_sample)
    tune_io(vm, stats)

    return stats


def translate(vm_stats):
    stats = {}

    for var in vm_stats:
        if var == "ioTune":
            value = vm_stats[var]
            if value:
                # Convert ioTune numbers to strings to avoid xml-rpc issue
                # with numbers bigger than int32_t
                for ioTune in value:
                    ioTune["ioTune"] = dict(
                        (k, convertToStr(v)) for k, v
                        in ioTune["ioTune"].iteritems())
                stats[var] = vm_stats[var]
        elif type(vm_stats[var]) is not dict:
            stats[var] = convertToStr(vm_stats[var])
        elif var in ('disks', 'network', 'balloonInfo'):
            value = vm_stats[var]
            if value:
                stats[var] = value

    return stats


def tune_io(vm, stats):
    """
    Collect the current ioTune settings for all disks VDSM knows about.

    This assumes VDSM always has the correct info and nobody else is
    touching the device without telling VDSM about it.

    TODO: We might want to move to XML parsing (first update) and events
    once libvirt supports them:
    https://bugzilla.redhat.com/show_bug.cgi?id=1114492
    """
    io_tune_info = []

    for disk in vm.getDiskDevices():
        if "ioTune" in disk.specParams:
            io_tune_info.append({
                "name": disk.name,
                "path": disk.path,
                "ioTune": disk.specParams["ioTune"]
            })

    stats['ioTune'] = io_tune_info


def cpu(stats, first_sample, last_sample, interval):
    """
    Add cpu statistics to the `stats' dict:
    - cpuUser
    - cpuSys
    - cpuTime
    Expect two samplings `first_sample' and `last_sample'
    which must be data in the format of the libvirt bulk stats.
    `interval' is the time between the two samplings, in seconds.
    Fill `stats' as much as possible, bailing out at first error.
    Return None on error,  if any needed data is missing or wrong.
    Return the `stats' dictionary on success.
    """
    stats['cpuUser'] = 0.0
    stats['cpuSys'] = 0.0

    if first_sample is None or last_sample is None:
        return None
    if interval <= 0:
        logging.warning(
            'invalid interval %i when computing CPU stats',
            interval)
        return None

    keys = ('cpu.system', 'cpu.user')
    samples = (last_sample, first_sample)

    if all(k in s for k in keys for s in samples):
        # TODO: cpuUsage should have the same type as cpuUser and cpuSys.
        # we may block the str() when xmlrpc is deserted.
        stats['cpuUsage'] = str(last_sample['cpu.system'] +
                                last_sample['cpu.user'])

        cpu_sys = ((last_sample['cpu.user'] - first_sample['cpu.user']) +
                   (last_sample['cpu.system'] - first_sample['cpu.system']))
        stats['cpuSys'] = _usage_percentage(cpu_sys, interval)

        if all('cpu.time' in s for s in samples):
            stats['cpuUser'] = _usage_percentage(
                ((last_sample['cpu.time'] - first_sample['cpu.time']) -
                 cpu_sys),
                interval)

            return stats

    return None


def balloon(vm, stats, sample):
    max_mem = int(vm.conf.get('memSize')) * 1024

    for dev in vm.getBalloonDevicesConf():
        if dev['specParams']['model'] != 'none':
            balloon_target = dev.get('target', max_mem)
            break
    else:
        balloon_target = None

    stats['balloonInfo'] = {}

    # Do not return any balloon status info before we get all data
    # MOM will ignore VMs with missing balloon information instead
    # using incomplete data and computing wrong balloon targets
    if balloon_target is not None and sample is not None:

        balloon_cur = 0
        with _skip_if_missing_stats(vm):
            balloon_cur = sample['balloon.current']

        stats['balloonInfo'].update({
            'balloon_max': str(max_mem),
            'balloon_min': str(
                int(vm.conf.get('memGuaranteedSize', '0')) * 1024),
            'balloon_cur': str(balloon_cur),
            'balloon_target': str(balloon_target)
        })


def cpu_count(stats, sample):
    # Handling the case when not enough samples exist
    if sample is None:
        return

    if 'vcpu.current' in sample:
        vcpu_count = sample['vcpu.current']
        if vcpu_count != -1:
            stats['vcpuCount'] = vcpu_count
        else:
            logging.error('Failed to get VM cpu count')


def report_stats(vms_stats):
    report = {}
    try:
        for vm_uuid in vms_stats:
            prefix = "vms." + vm_uuid
            stat = vms_stats[vm_uuid]
            report[prefix + '.cpu.user'] = stat['cpuUser']
            report[prefix + '.cpu.sys'] = stat['cpuSys']
            report[prefix + '.cpu.usage'] = stat['cpuUsage']

            report[prefix + '.balloon.max'] = \
                stat['balloonInfo']['balloon_max']
            report[prefix + '.balloon.min'] = \
                stat['balloonInfo']['balloon_min']
            report[prefix + '.balloon.target'] = \
                stat['balloonInfo']['balloon_target']
            report[prefix + '.balloon.cur'] = \
                stat['balloonInfo']['balloon_cur']

            if 'disks' in stat:
                for disk in stat['disks']:
                    diskprefix = prefix + '.vm_disk.' + disk
                    diskinfo = stat['disks'][disk]

                    report[diskprefix + '.read.latency'] = \
                        diskinfo['readLatency']
                    report[diskprefix + '.read.ops'] = \
                        diskinfo['readOps']
                    report[diskprefix + '.read.bytes'] = \
                        diskinfo['readBytes']
                    report[diskprefix + '.read.rate'] = \
                        diskinfo['readRate']

                    report[diskprefix + '.write.bytes'] = \
                        diskinfo['writtenBytes']
                    report[diskprefix + '.write.ops'] = \
                        diskinfo['writeOps']
                    report[diskprefix + '.write.latency'] = \
                        diskinfo['writeLatency']
                    report[diskprefix + '.write.rate'] = \
                        diskinfo['writeRate']

                    report[diskprefix + '.apparent_size'] = \
                        diskinfo['apparentsize']
                    report[diskprefix + '.flush_latency'] = \
                        diskinfo['flushLatency']
                    report[diskprefix + '.true_size'] = \
                        diskinfo['truesize']

            if 'network' in stat:
                for interface in stat['network']:
                    netprefix = prefix + '.network_interfaces.' + interface
                    if_info = stat['network'][interface]
                    report[netprefix + '.speed'] = if_info['speed']
                    report[netprefix + '.rx.bytes'] = if_info['rx']
                    report[netprefix + '.rx.errors'] = if_info['rxErrors']
                    report[netprefix + '.rx.dropped'] = if_info['rxDropped']

                    report[netprefix + '.tx.bytes'] = if_info['tx']
                    report[netprefix + '.tx.errors'] = if_info['txErrors']
                    report[netprefix + '.tx.dropped'] = if_info['txDropped']

        # Guest cpu-count,apps list, status, mac addr, client IP,
        # display type, kvm enabled, username, vcpu info, vm jobs,
        # displayinfo, hash, acpi, fqdn, vm uuid, pid, vNodeRuntimeInfo,
        #
        # are all meta-data that should be published separately

        metrics.send(report)
    except KeyError:
        logging.exception('Report vm stats failed')


def _nic_traffic(vm_obj, name, model, mac,
                 start_sample, start_index,
                 end_sample, end_index, interval):
    """
    Return per-nic statistics packed into a dictionary
    - macAddr
    - name
    - speed
    - state
    - {rx,tx}Errors
    - {rx,tx}Dropped
    - {rx,tx}Rate
    - {rx,tx}
    - sampleTime
    Produce as many statistics as possible, skipping errors.
    Expect two samplings `start_sample' and `end_sample'
    which must be data in the format of the libvirt bulk stats.
    Expects the indexes of the nic whose statistics needs to be produced,
    for each sampling:
    `start_index' for `start_sample', `end_index' for `end_sample'.
    `interval' is the time between the two samplings, in seconds.
    `vm_obj' is the Vm instance to which the nic belongs.
    `name', `model' and `mac' are the attributes of the said nic.
    Those three value are reported in the output stats.
    Return None on error,  if any needed data is missing or wrong.
    Return the `stats' dictionary on success.
    """

    if_speed = 1000 if model in ('e1000', 'virtio') else 100

    if_stats = {
        'macAddr': mac,
        'name': name,
        'speed': str(if_speed),
        'state': 'unknown',
    }

    with _skip_if_missing_stats(vm_obj):
        if_stats['rxErrors'] = str(end_sample['net.%d.rx.errs' % end_index])
        if_stats['rxDropped'] = str(end_sample['net.%d.rx.drop' % end_index])
        if_stats['txErrors'] = str(end_sample['net.%d.tx.errs' % end_index])
        if_stats['txDropped'] = str(end_sample['net.%d.tx.drop' % end_index])

    with _skip_if_missing_stats(vm_obj):
        if_stats['rx'] = str(end_sample['net.%d.rx.bytes' % end_index])
        if_stats['tx'] = str(end_sample['net.%d.tx.bytes' % end_index])
        rx_delta = (
            end_sample['net.%d.rx.bytes' % end_index] -
            start_sample['net.%d.rx.bytes' % start_index]
        )
        tx_delta = (
            end_sample['net.%d.tx.bytes' % end_index] -
            start_sample['net.%d.tx.bytes' % start_index]
        )

        if_rx_bytes = (100.0 *
                       (rx_delta % 2 ** 32) /
                       interval / if_speed / _MBPS_TO_BPS)
        if_tx_bytes = (100.0 *
                       (tx_delta % 2 ** 32) /
                       interval / if_speed / _MBPS_TO_BPS)

        if_stats['rxRate'] = '%.1f' % if_rx_bytes
        if_stats['txRate'] = '%.1f' % if_tx_bytes

    if_stats['sampleTime'] = monotonic_time()

    return if_stats


def networks(vm, stats, first_sample, last_sample, interval):
    stats['network'] = {}

    if first_sample is None or last_sample is None:
        return None
    if interval <= 0:
        logging.warning(
            'invalid interval %i when computing network stats for vm %s',
            interval, vm.id)
        return None

    first_indexes = _find_bulk_stats_reverse_map(first_sample, 'net')
    last_indexes = _find_bulk_stats_reverse_map(last_sample, 'net')

    for nic in vm.getNicDevices():
        if nic.name.startswith('hostdev'):
            continue

        # may happen if nic is a new hot-plugged one
        if nic.name not in first_indexes or nic.name not in last_indexes:
            continue

        stats['network'][nic.name] = _nic_traffic(
            vm, nic.name, nic.nicModel, nic.macAddr,
            first_sample, first_indexes[nic.name],
            last_sample, last_indexes[nic.name],
            interval)

    return stats


def disks(vm, stats, first_sample, last_sample, interval):
    if first_sample is None or last_sample is None:
        return None

    # libvirt does not guarantee that disk will returned in the same
    # order across calls. It is usually like this, but not always,
    # for example if hotplug/hotunplug comes into play.
    # To be safe, we need to find the mapping after each call.
    first_indexes = _find_bulk_stats_reverse_map(first_sample, 'block')
    last_indexes = _find_bulk_stats_reverse_map(last_sample, 'block')
    disk_stats = {}

    for vm_drive in vm.getDiskDevices():
        drive_stats = {}
        try:
            drive_stats = {
                'truesize': str(vm_drive.truesize),
                'apparentsize': str(vm_drive.apparentsize),
                'readLatency': '0',
                'writeLatency': '0',
                'flushLatency': '0'
            }
            if isVdsmImage(vm_drive):
                drive_stats['imageID'] = vm_drive.imageID
            elif "GUID" in vm_drive:
                drive_stats['lunGUID'] = vm_drive.GUID

            if (vm_drive.name in first_indexes and
               vm_drive.name in last_indexes):
                # will be None if sampled during recovery
                if interval <= 0:
                    logging.warning(
                        'invalid interval %i when calculating '
                        'stats for vm %s disk %s',
                        interval, vm.id, vm_drive.name)
                else:
                    drive_stats.update(
                        _disk_rate(
                            first_sample, first_indexes[vm_drive.name],
                            last_sample, last_indexes[vm_drive.name],
                            interval))
                drive_stats.update(
                    _disk_latency(
                        first_sample, first_indexes[vm_drive.name],
                        last_sample, last_indexes[vm_drive.name]))
                drive_stats.update(
                    _disk_iops_bytes(
                        first_sample, first_indexes[vm_drive.name],
                        last_sample, last_indexes[vm_drive.name]))

        except AttributeError:
            logging.exception("Disk %s stats not available",
                              vm_drive.name)

        disk_stats[vm_drive.name] = drive_stats

    if disk_stats:
        stats['disks'] = disk_stats

    return stats


def _disk_rate(first_sample, first_index, last_sample, last_index, interval):
    stats = {}

    for name, mode in (("readRate", "rd"), ("writeRate", "wr")):
        first_key = 'block.%d.%s.bytes' % (first_index, mode)
        last_key = 'block.%d.%s.bytes' % (last_index, mode)
        try:
            first_value = first_sample[first_key]
            last_value = last_sample[last_key]
        except KeyError:
            continue
        stats[name] = str((last_value - first_value) / interval)

    return stats


def _disk_latency(first_sample, first_index, last_sample, last_index):
    stats = {}

    for name, mode in (('readLatency', 'rd'),
                       ('writeLatency', 'wr'),
                       ('flushLatency', 'fl')):
        try:
            last_key = "block.%d.%s" % (last_index, mode)
            first_key = "block.%d.%s" % (first_index, mode)
            operations = (last_sample[last_key + ".reqs"] -
                          first_sample[first_key + ".reqs"])
            elapsed_time = (last_sample[last_key + ".times"] -
                            first_sample[first_key + ".times"])
        except KeyError:
            continue
        if operations:
            stats[name] = str(elapsed_time / operations)
        else:
            stats[name] = '0'

    return stats


def _disk_iops_bytes(first_sample, first_index, last_sample, last_index):
    stats = {}

    for name, mode, field in (('readOps', 'rd', 'reqs'),
                              ('writeOps', 'wr', 'reqs'),
                              ('readBytes', 'rd', 'bytes'),
                              ('writtenBytes', 'wr', 'bytes')):
        key = 'block.%d.%s.%s' % (last_index, mode, field)
        try:
            value = last_sample[key]
        except KeyError:
            continue
        stats[name] = str(value)

    return stats


def _usage_percentage(val, interval):
    return 100 * val / interval / 1000 ** 3


def _find_bulk_stats_reverse_map(stats, group):
    name_to_idx = {}
    for idx in six.moves.xrange(stats.get('%s.count' % group, 0)):
        try:
            name = stats['%s.%d.name' % (group, idx)]
        except KeyError:
            # Bulk stats accumulate what they can get, raising errors
            # only in the critical cases. This includes fundamental
            # attributes like names, so count has to be considered
            # an upper bound more like a precise indicator.
            pass
        else:
            name_to_idx[name] = idx
    return name_to_idx


@contextlib.contextmanager
def _skip_if_missing_stats(vm_obj):
    """
    Depending on the VM state, some exceptions while accessing
    the bulk stats samples are to be expected, and harmless.
    This context manager swallows those and let the others
    bubble up.
    """
    try:
        yield
    except KeyError:
        if vm_obj.incomingMigrationPending():
            # If a VM is migration destination,
            # libvirt doesn't give any disk stat.
            pass
        else:
            raise
