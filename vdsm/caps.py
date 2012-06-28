#
# Copyright 2011 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
#
# Refer to the README and COPYING files for full details of the license
#

"""Collect host capabilities"""

import os
from xml.dom import minidom
import subprocess
import logging
import time
import struct
import socket
import itertools
import linecache
import glob

import libvirt

from vdsm.config import config
from vdsm import libvirtconnection
import dsaversion
from vdsm import netinfo
import hooks
from vdsm import utils
from vdsm import constants
import storage.hba

# For debian systems we can use python-apt if available
try:
  import apt
  python_apt = True
except ImportError:
  python_apt = False


class OSName:
    UNKNOWN = 'unknown'
    OVIRT = 'oVirt Node'
    RHEL = 'RHEL'
    FEDORA = 'Fedora'
    RHEVH = 'RHEV Hypervisor'
    DEBIAN = 'Debian'

class CpuInfo(object):
    def __init__(self, cpuinfo='/proc/cpuinfo'):
        """Parse /proc/cpuinfo"""
        self._info = {}
        p = {}
        for line in file(cpuinfo):
            if line.strip() == '':
                p = {}
                continue
            key, value = map(str.strip, line.split(':', 1))
            if key == 'processor':
                self._info[value] = p
            else:
                p[key] = value

    def cores(self):
        return len(set((p.get('core id', '0'), p.get('physical id', '0'))
                    for p in self._info.values()))

    def sockets(self):
        phys_ids = [ p.get('physical id', '0') for p in self._info.values() ]
        return len(set(phys_ids))

    def flags(self):
        return self._info.itervalues().next()['flags'].split()

    def mhz(self):
        return self._info.itervalues().next()['cpu MHz']

    def model(self):
        return self._info.itervalues().next()['model name']

@utils.memoized
def _getEmulatedMachines():
    c = libvirtconnection.get()
    caps = minidom.parseString(c.getCapabilities())
    guestTag = caps.getElementsByTagName('guest')
    # Guest element is missing if kvm modules are not loaded
    if len(guestTag) == 0:
        return []

    guestTag = guestTag[0]

    return [ m.firstChild.toxml() for m in guestTag.getElementsByTagName('machine') ]

@utils.memoized
def _getCompatibleCpuModels():
    c = libvirtconnection.get()
    cpu_map = minidom.parseString(
                    file('/usr/share/libvirt/cpu_map.xml').read())
    def vendor(modelElem):
        vs = modelElem.getElementsByTagName('vendor')
        if vs:
            return vs[0].getAttribute('name')
        else:
            return None
    allModels = [ (m.getAttribute('name'), vendor(m)) for m
          in cpu_map.getElementsByTagName('arch')[0].childNodes
          if m.nodeName == 'model' ]
    def compatible(model, vendor):
        if not vendor:
            return False
        xml = '<cpu match="minimum"><model>%s</model>' \
              '<vendor>%s</vendor></cpu>' % (model, vendor)
        try:
            return c.compareCPU(xml, 0) in (
                                libvirt.VIR_CPU_COMPARE_SUPERSET,
                                libvirt.VIR_CPU_COMPARE_IDENTICAL)
        except libvirt.libvirtError, e:
            # hack around libvirt BZ#795836
            if e.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                return False
            raise

    return [ 'model_' + model for (model, vendor)
             in allModels if compatible(model, vendor) ]

def _parseKeyVal(lines, delim='='):
    d = {}
    for line in lines:
        kv = line.split(delim, 1)
        if len(kv) != 2:
            continue
        k, v = map(str.strip, kv)
        d[k] = v
    return d

def _getIscsiIniName():
    try:
        return _parseKeyVal(
                    file('/etc/iscsi/initiatorname.iscsi') )['InitiatorName']
    except:
        logging.error('reporting empty InitiatorName', exc_info=True)
    return ''

def getos():
    if os.path.exists('/etc/rhev-hypervisor-release'):
        return OSName.RHEVH
    elif glob.glob('/etc/ovirt-node-*-release'):
        return OSName.OVIRT
    elif os.path.exists('/etc/fedora-release'):
        return OSName.FEDORA
    elif os.path.exists('/etc/redhat-release'):
        return OSName.RHEL
    elif os.path.exists('/etc/debian_version'):
        return OSName.DEBIAN
    else:
        return OSName.UNKNOWN

__osversion = None
def osversion():
    global __osversion
    if __osversion is not None:
        return __osversion

    version = release = ''

    osname = getos()
    try:
        if osname == OSName.RHEVH:
            d = _parseKeyVal( file('/etc/default/version') )
            version = d.get('VERSION', '')
            release = d.get('RELEASE', '')
        elif osname == OSName.DEBIAN:
            version = linecache.getline('/etc/debian_version', 1).strip("\n")
            release = "" # Debian just has a version entry
        else:
            p = subprocess.Popen([constants.EXT_RPM, '-qf', '--qf',
                '%{VERSION} %{RELEASE}\n', '/etc/redhat-release'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, close_fds=True)
            out, err = p.communicate()
            if p.returncode == 0:
                version, release = out.splitlines()[-1].split()
    except:
        logging.error('failed to find version/release', exc_info=True)

    __osversion = dict(release=release, version=version, name=osname)
    return __osversion

def get():
    caps = {}

    caps['kvmEnabled'] = \
                str(config.getboolean('vars', 'fake_kvm_support') or
                    os.path.exists('/dev/kvm')).lower()

    cpuInfo =  CpuInfo()
    caps['cpuCores'] = str(cpuInfo.cores())
    caps['cpuSockets'] = str(cpuInfo.sockets())
    caps['cpuSpeed'] = cpuInfo.mhz()
    if config.getboolean('vars', 'fake_kvm_support'):
        caps['cpuModel'] = 'Intel(Fake) CPU'
        flags = set(cpuInfo.flags() + ['vmx', 'sse2', 'nx'])
        caps['cpuFlags'] = ','.join(flags) + 'model_486,model_pentium,' \
            'model_pentium2,model_pentium3,model_pentiumpro,model_qemu32,' \
            'model_coreduo,model_core2duo,model_n270,model_Conroe,' \
            'model_Penryn,model_Nehalem,model_Opteron_G1'
    else:
        caps['cpuModel'] = cpuInfo.model()
        caps['cpuFlags'] = ','.join(cpuInfo.flags() +
                                    _getCompatibleCpuModels())

    caps.update(dsaversion.version_info)
    caps.update(netinfo.get())

    try:
        caps['hooks'] = hooks.installed()
    except:
        logging.debug('not reporting hooks', exc_info=True)

    caps['operatingSystem'] = osversion()
    caps['uuid'] = utils.getHostUUID()
    caps['packages2'] = _getKeyPackages()
    caps['emulatedMachines'] = _getEmulatedMachines()
    caps['ISCSIInitiatorName'] = _getIscsiIniName()
    caps['HBAInventory'] = storage.hba.HBAInventory()
    caps['vmTypes'] = ['kvm']

    caps['memSize'] = str(utils.readMemInfo()['MemTotal'] / 1024)
    caps['reservedMem'] = str(
            config.getint('vars', 'host_mem_reserve') +
            config.getint('vars', 'extra_mem_reserve') )
    caps['guestOverhead'] = config.get('vars', 'guest_ram_overhead')

    return caps

def _getIfaceByIP(addr):
    remote = struct.unpack('I', socket.inet_aton(addr))[0]
    for line in itertools.islice(file('/proc/net/route'), 1, None):
        iface, dest, gateway, flags, refcnt, use, metric, \
                mask, mtu, window, irtt = line.split()
        dest = int(dest, 16)
        mask = int(mask, 16)
        if remote & mask == dest & mask:
            return iface
    return '' # should never get here w/ default gw

def _getKeyPackages():
    def kernelDict():
        try:
            ver, rel = file('/proc/sys/kernel/osrelease').read(). \
                                strip().split('-', 1)
        except:
            logging.error('kernel release not found', exc_info=True)
            ver, rel = '0', '0'
        try:
            t = file('/proc/sys/kernel/version').read().split()[2:]
            del t[4] # Delete timezone
            t = time.mktime(time.strptime(' '.join(t)))
        except:
            logging.error('kernel build time not found', exc_info=True)
            t = '0'
        return dict(version=ver, release=rel, buildtime=t)

    pkgs = {'kernel': kernelDict()}

    if getos() in (OSName.RHEVH, OSName.OVIRT, OSName.FEDORA, OSName.RHEL):
        KEY_PACKAGES = ['qemu-kvm', 'qemu-img',
                        'vdsm', 'spice-server', 'libvirt']

        try:
            for pkg in KEY_PACKAGES:
                rc, out, err = utils.execCmd([constants.EXT_RPM, '-q', '--qf',
                      '%{NAME}\t%{VERSION}\t%{RELEASE}\t%{BUILDTIME}\n', pkg],
                      sudo=False)
                if rc: continue
                line = out[-1]
                n, v, r, t = line.split()
                pkgs[pkg] = dict(version=v, release=r, buildtime=t)
        except:
            logging.error('', exc_info=True)

    elif getos() == OSName.DEBIAN and python_apt == True:
        KEY_PACKAGES = { 'qemu-kvm':'qemu-kvm', 'qemu-img':'qemu-utils',
                         'vdsm':'vdsmd', 'spice-server':'libspice-server1',
                         'libvirt':'libvirt0' }

        cache = apt.Cache()

        for pkg in KEY_PACKAGES:
            try:
                deb_pkg = KEY_PACKAGES[pkg]
                ver = cache[deb_pkg].installed.version
                pkgs[pkg] = dict(version=ver, release="", buildtime="") # Debian just offers a version
            except:
                logging.error('', exc_info=True)

    return pkgs

