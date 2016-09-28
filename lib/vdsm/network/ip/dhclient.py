# Copyright (C) 2013, IBM Corporation
# Copyright (C) 2013-2014, Red Hat, Inc.
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

import errno
import logging
import os
import subprocess
import threading

from vdsm import cmdutils
from vdsm.network import errors as ne
from vdsm.network.link import iface as linkiface
from vdsm.commands import execCmd
from vdsm.utils import CommandPath, memoized, pgrep, kill_and_rm_pid

DHCLIENT_BINARY = CommandPath('dhclient', '/sbin/dhclient')
DHCLIENT_CGROUP = 'vdsm-dhclient'
LEASE_DIR = '/var/lib/dhclient'
LEASE_FILE = os.path.join(LEASE_DIR, 'dhclient{0}--{1}.lease')


class DhcpClient(object):
    PID_FILE = '/var/run/dhclient%s-%s.pid'

    def __init__(self, iface, family=4, default_route=False, duid_source=None,
                 cgroup=DHCLIENT_CGROUP):
        self.iface = iface
        self.family = family
        self.default_route = default_route
        self.duid_source_file = None if duid_source is None else (
            LEASE_FILE.format('' if family == 4 else '6', duid_source))
        self.pidFile = self.PID_FILE % (family, self.iface)
        if not os.path.exists(LEASE_DIR):
            os.mkdir(LEASE_DIR)
        self.leaseFile = LEASE_FILE.format(
            '' if family == 4 else '6', self.iface)
        self._cgroup = cgroup

    def _dhclient(self):
        if linkiface.exists(self.iface):
            kill(self.iface, self.family)
        cmd = [DHCLIENT_BINARY.cmd, '-%s' % self.family, '-1', '-pf',
               self.pidFile, '-lf', self.leaseFile]
        if not self.default_route:
            # Instruct Fedora/EL's dhclient-script not to set gateway on iface
            cmd += ['-e', 'DEFROUTE=no']
        if self.duid_source_file and supports_duid_file():
            cmd += ['-df', self.duid_source_file]
        cmd += [self.iface]
        cmd = cmdutils.systemd_run(cmd, scope=True, slice=self._cgroup)
        return execCmd(cmd)

    def start(self, blocking):
        if blocking:
            return self._dhclient()
        else:
            t = threading.Thread(target=self._dhclient, name='vdsm-dhclient-%s'
                                 % self.iface)
            t.daemon = True
            t.start()

    def shutdown(self):
        try:
            pid = int(open(self.pidFile).readline().strip())
        except IOError as e:
            if e.errno == os.errno.ENOENT:
                pass
            else:
                raise
        else:
            kill_and_rm_pid(pid, self.pidFile)


def kill(device_name, family=4):
    for pid, pid_file in _pid_lookup(device_name, family):
        logging.info('Stopping dhclient -%s on %s', family, device_name)
        kill_and_rm_pid(pid, pid_file)


def is_active(device_name, family):
    for pid, _ in _pid_lookup(device_name, family):
        return True
    return False


def _pid_lookup(device_name, family):
    for pid in pgrep('dhclient'):
        try:
            with open('/proc/%s/cmdline' % pid) as cmdline:
                args = cmdline.read().strip('\0').split('\0')
        except IOError as ioe:
            if ioe.errno == errno.ENOENT:  # exited before we read cmdline
                continue
        if args[-1] != device_name:  # dhclient of another device
            continue
        tokens = iter(args)
        pid_file = '/var/run/dhclient.pid'  # Default client pid location
        running_family = 4
        for token in tokens:
            if token == '-pf':
                pid_file = next(tokens)
            elif token == '--no-pid':
                pid_file = None
            elif token == '-6':
                running_family = 6

        if running_family != family:
            continue

        yield pid, pid_file


@memoized
def supports_duid_file():
    """
    On EL7 dhclient doesn't have the -df option (to read the DUID from a bridge
    port's lease file). We must detect if the option is available, by running
    dhclient manually. To support EL7, we should probably fall back to -lf and
    refer dhclient to a new lease file with a device name substituted.
    """
    probe = subprocess.Popen(
        [DHCLIENT_BINARY.cmd,  # dhclient doesn't have -h/--help
         '-do-you-support-loading-duid-from-lease-files?'],
        stderr=subprocess.PIPE)

    _, err = probe.communicate()
    return '-df' in err


def run(iface, family=4, default_route=False, duid_source=None,
        blocking_dhcp=False):
    dhclient = DhcpClient(iface, family, default_route, duid_source)
    ret = dhclient.start(blocking_dhcp)
    if blocking_dhcp and ret[0]:
        raise ne.ConfigNetworkError(
            ne.ERR_FAILED_IFUP, 'dhclient%s failed' % family)


def stop(iface):
    dhclient = DhcpClient(iface)
    dhclient.shutdown()
