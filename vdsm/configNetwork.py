# Copyright 2011-2012 Red Hat, Inc.
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

import sys, subprocess, os, re, traceback
import shutil
import pipes
import pwd
import time
import logging
from collections import defaultdict
import threading
from xml.sax.saxutils import escape

import libvirt

from vdsm import constants
from vdsm import utils
import neterrors as ne
from vdsm import define
from vdsm.netinfo import NetInfo, NET_CONF_DIR, NET_CONF_BACK_DIR, LIBVIRT_NET_PREFIX
from vdsm import libvirtconnection

CONNECTIVITY_TIMEOUT_DEFAULT = 4
MAX_VLAN_ID = 4094
MAX_BRIDGE_NAME_LEN = 15
ILLEGAL_BRIDGE_CHARS = frozenset(':. \t')
NETPREFIX = 'vdsm-'

class ConfigNetworkError(Exception):
    def __init__(self, errCode, message):
        self.errCode = errCode
        self.message = message
        Exception.__init__(self, self.errCode, self.message)


def ipcalc(checkopt, s):
    "Validate an ip address (or netmask) using ipcalc"
    if not isinstance(s, basestring):
        return 0
    p = subprocess.Popen([constants.EXT_IPCALC, '-c', checkopt, s],
            close_fds=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    if err:
        logging.warn(err)
    return not p.returncode

def ifdown(iface):
    "Bring down an interface"
    p = subprocess.Popen([constants.EXT_IFDOWN, iface], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, close_fds=True)
    out, err = p.communicate()
    if out.strip():
        logging.info(out)
    if err.strip():
        logging.warn('\n'.join([line for line in err.splitlines()
                                if not line.endswith(' does not exist!')]))
    return p.returncode

def ifup(iface):
    "Bring up an interface"
    p = subprocess.Popen([constants.EXT_IFUP, iface], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, close_fds=True)
    out, err = p.communicate()
    if out.strip():
        logging.info(out)
    if err.strip():
        logging.warn(err)

def ifaceUsers(iface):
    "Returns a list of entities using the interface"
    _netinfo = NetInfo()
    users = set()
    for n, ndict in _netinfo.networks.iteritems():
        if ndict['bridged'] and iface in ndict['ports']:
            users.add(n)
        elif not ndict['bridged'] and iface == ndict['interface']:
            users.add(n)
    for b, bdict in _netinfo.bondings.iteritems():
        if iface in bdict['slaves']:
            users.add(b)
    for v, vdict in _netinfo.vlans.iteritems():
        if iface == vdict['iface']:
            users.add(v)
    return users

def nicOtherUsers(bridge, vlan, bonding, nic):
    "Returns a list of interfaces using a nic, other than the specified one (used for validation)"
    if bonding:
        owner = bonding
    elif vlan:
        owner = nic + '.' + vlan
    else:
        owner = bridge
    users = ifaceUsers(nic)
    if bonding:
        users.update(bondingOtherUsers(bridge, vlan, bonding))
    users.discard(owner)
    return users

def bondingOtherUsers(bridge, vlan, bonding):
    "Return a list of nics/interfaces using a bonding, other than the specified one (used for validation)"
    if vlan:
        owner = bonding + '.' + vlan
    else:
        owner = bridge
    users = ifaceUsers(bonding)
    users.discard(owner)
    return users

# This function must respect the order used in:
#
#   /etc/rc.d/init.d/network
#
#   echo -e "ifcfg-eth0\nifcfg-eth1" \
#       | sed -e '/ifcfg-[A-Za-z0-9#\._-]\+$/ { s/^ifcfg-//g;s/[0-9]/ &/}' \
#       | sort -k 1,1 -k 2n
#
# Relevant cases:
#   nicSort(["p33p2", "p33p10"]) => ["p33p10", "p33p2"]
#   nicSort(["p331", "p33p1"]) => ["p33p1", "p331"]
#
def nicSort(nics):
    "Return a list of nics/interfaces ordered by name"

    nics_list = []
    nics_rexp = re.compile("^(\D*)(\d*)(.*)$")

    for nic_name in nics:
        nic_sre = nics_rexp.match(nic_name)
        prefix, stridx, postfix = nic_sre.groups((nic_name, "0", ""))

        try:
            intidx = int(stridx)
        except ValueError:
            intidx = 0

        nics_list.append((prefix, intidx, stridx + postfix))

    return [x + z for x, y, z in sorted(nics_list)]

class ConfigWriter(object):
    NET_CONF_PREF = NET_CONF_DIR + 'ifcfg-'
    CONFFILE_HEADER = '# automatically generated by vdsm'
    DELETED_HEADER = '# original file did not exist'

    def __init__(self):
        self._backups = {}

    def _backup(self, filename):
        self._atomicBackup(filename)
        self._persistentBackup(filename)

    def _atomicBackup(self, filename):
        """Backs up configuration to memory, for a later rollback in case of error."""
        confFile = os.path.join(NET_CONF_DIR, filename)
        if confFile not in self._backups:
            try:
                self._backups[confFile] = open(confFile).read()
                logging.debug("Backed up %s" % confFile)
            except IOError:
                pass

    def restoreAtomicBackup(self):
        logging.info("Rolling back configuration (restoring atomic backup)")
        if not self._backups:
            return
        for confFile, content in self._backups.iteritems():
            open(confFile, 'w').write(content)
            logging.debug('Restored %s', confFile)
        subprocess.Popen(['/etc/init.d/network', 'start'])

    @staticmethod
    def _removeFile(filename):
        """Remove file, umounting ovirt config files if needed."""

        mounts = open('/proc/mounts').read()
        if ' /config ext3' in mounts and ' %s ext3' % filename in mounts:
            subprocess.call([constants.EXT_UMOUNT, '-n', filename])
        utils.rmFile(filename)

    @classmethod
    def _persistentBackup(cls, filename):
        if os.path.exists('/usr/libexec/ovirt-functions'):
            subprocess.call([constants.EXT_SH, '/usr/libexec/ovirt-functions', 'unmount_config', filename])
            logging.debug("unmounted %s using ovirt" % filename)

        (dummy, basename) = os.path.split(filename)
        backup = os.path.join(NET_CONF_BACK_DIR, basename)
        if os.path.exists(backup):
            # original copy already backed up
            return

        vdsm_uid = pwd.getpwnam('vdsm').pw_uid

        # make directory (if it doesn't exist) and assign it to vdsm
        if not os.path.exists(NET_CONF_BACK_DIR):
            os.mkdir(NET_CONF_BACK_DIR)
        os.chown(NET_CONF_BACK_DIR, vdsm_uid, 0)

        if os.path.exists(filename):
            shutil.copy2(filename, backup)
        else:
            open(backup, 'w').write(cls.DELETED_HEADER + '\n')
        os.chown(backup, vdsm_uid, 0)
        logging.debug("Persistently backed up %s (until next 'set safe config')" % filename)

    def addBridge(self, name, ipaddr=None, netmask=None, mtu=None,
            gateway=None, bootproto=None, delay='0', onboot='yes', **kwargs):
        "Based on addNetwork"

        s = """DEVICE=%s\nTYPE=Bridge\nONBOOT=%s\n""" % (pipes.quote(name), pipes.quote(onboot))
        if ipaddr:
            s = s + 'IPADDR=%s\nNETMASK=%s\n' % (pipes.quote(ipaddr), pipes.quote(netmask))
            if gateway:
                s = s + 'GATEWAY=%s\n' % pipes.quote(gateway)
        else:
            if bootproto:
                s = s + 'BOOTPROTO=%s\n' % pipes.quote(bootproto)
        if mtu:
            s = s + 'MTU=%d\n' % mtu
        s += 'DELAY=%s\n' % pipes.quote(delay)
        s += 'NM_CONTROLLED=no\n'
        BLACKLIST = ['TYPE', 'NAME', 'DEVICE', 'bondingOptions',
                     'force', 'blockingdhcp',
                     'connectivityCheck', 'connectivityTimeout',
                     'implicitBonding']
        for k in set(kwargs.keys()).difference(set(BLACKLIST)):
            if re.match('^[a-zA-Z_]\w*$', k):
                s += '%s=%s\n' % (k.upper(), pipes.quote(kwargs[k]))
            else:
                logging.debug('ignoring variable %s' % k)
        conffile = self.NET_CONF_PREF + name
        self._backup(conffile)
        open(conffile, 'w').write(s)
        os.chmod(conffile, 0664)

    def addVlan(self, vlanId, iface, network, mtu=None, bridged=True):
        "Based on addNetwork"
        conffile = self.NET_CONF_PREF + iface + '.' + vlanId
        self._backup(conffile)
        content = """DEVICE=%s.%s\nONBOOT=yes\nVLAN=yes\nBOOTPROTO=none\nNM_CONTROLLED=no\n"""
        if mtu:
            content = content + 'MTU=%d\n' % mtu
        if bridged:
            content = content + 'BRIDGE=%s\n' % pipes.quote(network)
        open(conffile, 'w').write(content % (pipes.quote(iface), vlanId))
        os.chmod(conffile, 0664)

    def addBonding(self, bonding, bridge=None, bondingOptions=None, mtu=None):
        "Based on addNetwork"
        conffile = self.NET_CONF_PREF + bonding
        self._backup(conffile)
        with open(conffile, 'w') as f:
            f.write("""DEVICE=%s\nONBOOT=yes\nBOOTPROTO=none\n""" % (bonding))
            if bridge:
                f.write('BRIDGE=%s\n' % pipes.quote(bridge))
            if not bondingOptions:
                bondingOptions = 'mode=802.3ad miimon=150'
            f.write('BONDING_OPTS=%s\n' % pipes.quote(bondingOptions or ''))
            f.write('NM_CONTROLLED=no\n')
            if mtu:
                f.write('MTU=%d\n' % mtu)
        os.chmod(conffile, 0664)
        # create the bonding device to avoid initscripts noise
        if bonding not in open('/sys/class/net/bonding_masters').read().split():
            open('/sys/class/net/bonding_masters', 'w').write('+%s\n' % bonding)

    def addNic(self, nic, bonding=None, bridge=None, mtu=None):
        "Based on addNetwork"
        conffile = self.NET_CONF_PREF + nic
        self._backup(conffile)
        _netinfo = NetInfo()
        hwaddr = _netinfo.nics[nic].get('permhwaddr') or \
                 _netinfo.nics[nic]['hwaddr']
        with open(conffile, 'w') as f:
            f.write('DEVICE=%s\nONBOOT=yes\nBOOTPROTO=none\nHWADDR=%s\n' % (pipes.quote(nic),
                    pipes.quote(hwaddr)))
            if bridge:
                f.write('BRIDGE=%s\n' % pipes.quote(bridge))
            if bonding:
                f.write('MASTER=%s\n' % pipes.quote(bonding))
                f.write('SLAVE=yes\n')
            f.write('NM_CONTROLLED=no\n')
            if mtu:
                f.write('MTU=%d\n' % mtu)
        os.chmod(conffile, 0664)

    def removeNic(self, nic):
        cf = self.NET_CONF_PREF + nic
        self._backup(cf)
        try:
            hwlines = [ line for line in open(cf).readlines()
                        if line.startswith('HWADDR=') ]
            l = ['DEVICE=%s\n' % nic, 'ONBOOT=yes\n', 'BOOTPROTO=none\n'] + hwlines
            open(cf, 'w').writelines(l)
        except IOError:
            pass

    def removeVlan(self, vlanId, iface):
        self._backup(self.NET_CONF_PREF + iface + '.' + vlanId)
        self._removeFile(self.NET_CONF_PREF + iface + '.' + vlanId)

    def removeBonding(self, bonding):
        self._backup(self.NET_CONF_PREF + bonding)
        self._removeFile(self.NET_CONF_PREF + bonding)

    def removeBridge(self, bridge):
        self._backup(self.NET_CONF_PREF + bridge)
        self._removeFile(self.NET_CONF_PREF + bridge)

    def _getConfigValue(self, conffile, entry):
        """
        Get value from network configuration file

        :param entry: entry to look for (entry=value)
        :type entry: string

        :returns: value for entry (or None)
        :rtype: string

        Search for entry in conffile and return
        its value or None if not found
        """
        with open(conffile) as f:
            entries = [ line for line in f.readlines()
                        if line.startswith(entry + '=') ]
        if len(entries) != 0:
            value = entries[0].split('=', 1)[1]
            return value.strip()
        return None

    def _updateConfigValue(self, conffile, entry, value, delete=False):
        """
        Set value for network configuration file

        :param entry: entry to update (entry=value)
        :type entry: string

        :param value: value to update (entry=value)
        :type value: string

        :param delete: delete entry
        :type delete: boolean

        Search for entry in conffile and return
        its value or None if not found,
        if delete is True the entry will be deleted from
        the configuration file
        """
        with open(conffile) as f:
            entries = [ line for line in f.readlines()
                        if not line.startswith(entry + '=') ]

        if not delete:
            entries.append('\n' + entry + '=' + value)

        self._backup(conffile)
        try:
            with open(conffile, 'w') as f:
                f.writelines(entries)
                f.close()
        except:
            self.restoreAtomicBackup()
            raise

    def getMaxMtu(self, nics, mtu):
        """
        Get the max MTU value from configuration/parameter

        :param nics: list of nics
        :type nics: list

        :param mtu: mtu value
        :type mtu: integer

        getMaxMtu return the highest value in a connection tree,
        it check if a vlan, bond that have a higher mtu value
        """
        for nic in nics:
            cf = self.NET_CONF_PREF + nic
            mtuval = self._getConfigValue(cf, 'MTU')
            if not mtuval is None:
                if int(mtuval) > mtu:
                    mtu = mtuval
        return mtu

    def setNewMtu(self, bridge):
        """
        Set new MTU value to bridge and its interfaces

        :param bridge: bridge name
        :type bridge: string

        Update MTU to devices (bridge, interfaces, bonds and vlans)
        Or added a new value,
        also set the bridge to the higher value if its under vlans or bond
        """
        _netinfo = NetInfo()
        cf = self.NET_CONF_PREF + bridge
        currmtu = self._getConfigValue(cf, 'MTU')
        if currmtu is None:
            return

        nics, delvlan, bonding = _netinfo.getNicsVlanAndBondingForNetwork(bridge)
        if delvlan is None:
            return

        if bonding:
            _, vlans = _netinfo.getNetworksAndVlansForBonding(bonding)
            delvlan = bonding + '.' + delvlan
        else:
            vlans = _netinfo.getVlansForNic(nics[0])
            delvlan = nics[0] + '.' + delvlan

        newmtu = None
        for vlan in vlans:
            if vlan == delvlan:
                continue
            cf = self.NET_CONF_PREF + vlan
            mtu = self._getConfigValue(cf, 'MTU')
            newmtu = max(newmtu, mtu)

        if newmtu != currmtu:
            if bonding:
                slaves = NetInfo.slaves(bonding)
                for slave in slaves:
                    cf = self.NET_CONF_PREF + slave
                    self._updateConfigValue(cf, 'MTU', newmtu, newmtu is None)
            else:
                cf = self.NET_CONF_PREF + nics[0]
                self._updateConfigValue(cf, 'MTU', newmtu, newmtu is None)

def isBridgeNameValid(bridgeName):
    return bridgeName and len(bridgeName) <= MAX_BRIDGE_NAME_LEN and \
           len(set(bridgeName) & ILLEGAL_BRIDGE_CHARS) == 0 and \
           not bridgeName.startswith('-')

def validateBridgeName(bridgeName):
    if not isBridgeNameValid(bridgeName):
        raise ConfigNetworkError(ne.ERR_BAD_BRIDGE, "Bridge name isn't valid: %r"%bridgeName)

def validateIpAddress(ipAddr):
    if not ipcalc('-4', ipAddr):
        raise ConfigNetworkError(ne.ERR_BAD_ADDR, "Bad IP address: %r"%ipAddr)

def validateNetmask(netmask):
    if not ipcalc('-m', netmask):
        raise ConfigNetworkError(ne.ERR_BAD_ADDR, "Bad netmask: %r"%netmask)

def validateGateway(gateway):
    if not ipcalc('-4', gateway):
        raise ConfigNetworkError(ne.ERR_BAD_ADDR, "Bad gateway: %r"%gateway)

def validateBondingName(bonding):
    if not re.match('^bond[0-9]+$', bonding):
        raise ConfigNetworkError(ne.ERR_BAD_BONDING, '%r is not a valid bonding device name' % bonding)

def validateBondingOptions(bonding, bondingOptions):
    'Example: BONDING_OPTS="mode=802.3ad miimon=150"'
    try:
        for option in bondingOptions.split():
            key,value = option.split('=')
            if not os.path.exists('/sys/class/net/%(bonding)s/bonding/%(key)s'
                                  % locals()):
                raise ConfigNetworkError(ne.ERR_BAD_BONDING,
                        "%r is not a valid bonding option" % key)
    except ValueError:
        raise ConfigNetworkError(ne.ERR_BAD_BONDING,
                "Error parsing bonding options: %r" % bondingOptions)

def validateVlanId(vlan):
    try:
        if not 0 <= int(vlan) <= MAX_VLAN_ID:
            raise ConfigNetworkError(ne.ERR_BAD_VLAN, 'vlan id out of range: %r, must be 0..%s' % (vlan, MAX_VLAN_ID))
    except ValueError:
        raise ConfigNetworkError(ne.ERR_BAD_VLAN, 'vlan id must be a number')


def _addNetworkValidation(_netinfo, bridge, vlan, bonding, nics, ipaddr,
                          netmask, gateway, bondingOptions, bridged=True,
                          implicitBonding=False):
    # The (relatively) new setupNetwork verb allows to specify a network on
    # top of an existing bonding device. The nics of this bonds are taken
    # implictly from current host configuration
    if bonding and implicitBonding:
        pass
    elif (vlan or bonding) and not nics:
        raise ConfigNetworkError(ne.ERR_BAD_PARAMS,
                'vlan/bonding definition requires nics. got: %r' % (nics,))

    # Check bridge
    if bridged:
        validateBridgeName(bridge)
        if bridge in _netinfo.networks:
            raise ConfigNetworkError(ne.ERR_USED_BRIDGE, 'Bridge already exists')

        if bridge in _netinfo.getBridgelessNetworks():
            raise ConfigNetworkError(ne.ERR_USED_BRIDGE, 'network already exists')

    # vlan
    if vlan:
        validateVlanId(vlan)

    if bonding:
        validateBondingName(bonding)
        if bondingOptions:
            validateBondingOptions(bonding, bondingOptions)
    elif bondingOptions:
        raise ConfigNetworkError(ne.ERR_BAD_BONDING, 'Bonding options specified without bonding')

    # Check ip, netmask, gateway
    if ipaddr:
        if not netmask:
            raise ConfigNetworkError(ne.ERR_BAD_ADDR, "Must specify netmask to configure ip for network")
        validateIpAddress(ipaddr)
        validateNetmask(netmask)
        if gateway:
            validateGateway(gateway)
    else:
        if netmask or gateway:
            raise ConfigNetworkError(ne.ERR_BAD_ADDR, "Specified netmask or gateway but not ip")

    # Check nics
    for nic in nics:
        if nic not in _netinfo.nics:
            raise ConfigNetworkError(ne.ERR_BAD_NIC, "unknown nic: %r"%nic)

        bridgesForNic = list(_netinfo.getNetworksForNic(nic))
        if bridgesForNic:
            assert len(bridgesForNic) == 1
            raise ConfigNetworkError(ne.ERR_USED_NIC, "nic %r is already bound to network %r"%(nic, bridgesForNic[0]))

    if bonding and not vlan:
        for nic in nics:
            vlansForNic = list(_netinfo.getVlansForNic(nic))
            if len(vlansForNic):
                raise ConfigNetworkError(ne.ERR_USED_NIC, 'nic %s already used by vlans %s' % ( nics, vlansForNic))

    # Bonding
    if bonding:
        bonding_ifaces = _netinfo.getNetworksAndVlansForBonding(bonding)
        if vlan:    # Make sure all connected interfaces (if any) are vlans
            for (bonding_bridge, bonding_vlan) in bonding_ifaces:
                if bonding_vlan is None:
                    raise ConfigNetworkError(ne.ERR_BAD_BONDING, 'bonding %r is already member of network %r'%(
                                             bonding, bonding_bridge ))
        else:
            bonding_ifaces = list(bonding_ifaces)
            if len(bonding_ifaces):
                raise ConfigNetworkError(ne.ERR_BAD_BONDING, 'bonding %r already has members: %r'%(bonding,bonding_ifaces))

    else:
        if len(nics) > 1:
            raise ConfigNetworkError(ne.ERR_BAD_BONDING, 'multiple nics require a bonding device')

    # Make sure nics don't have a different bonding
    # still relevant if bonding is None
    for nic in nics:
        bondingForNics = _netinfo.getBondingForNic(nic)
        if bondingForNics and bondingForNics != bonding:
            raise ConfigNetworkError(ne.ERR_USED_NIC, 'nic %s already enslaved to %s' % (nic, bondingForNics))

def addNetwork(network, vlan=None, bonding=None, nics=None, ipaddr=None, netmask=None, mtu=None,
               gateway=None, force=False, configWriter=None, bondingOptions=None, bridged=True, **options):
    nics = nics or ()
    _netinfo = NetInfo()
    bridged = utils.tobool(bridged)

    if mtu:
        mtu = int(mtu)

    # Validation
    if not utils.tobool(force):
        logging.debug('validating bridge...')
        _addNetworkValidation(_netinfo, bridge=network if bridged else None,
                vlan=vlan, bonding=bonding, nics=nics, ipaddr=ipaddr,
                netmask=netmask, gateway=gateway, bondingOptions=bondingOptions,
                bridged=bridged, **options)

    logging.info("Adding network %s with vlan=%s, bonding=%s, nics=%s,"
                 " bondingOptions=%s, mtu=%s, bridged=%s, options=%s",
                 network, vlan, bonding, nics, bondingOptions,
                 mtu, bridged, options)

    if configWriter is None:
        configWriter = ConfigWriter()

    prevmtu = None
    if mtu and vlan:
        prevmtu = configWriter.getMaxMtu(nics, mtu)

    nic = nics[0] if nics else None
    iface = bonding or nic

    # take down nics that need to be changed
    vlanedIfaces = [v['iface'] for v in _netinfo.vlans.values()]
    if bonding not in vlanedIfaces:
        for nic in nics:
            if nic not in vlanedIfaces:
                ifdown(nic)

    if bridged:
        configWriter.addBridge(network, ipaddr=ipaddr, netmask=netmask,
                                mtu=mtu, gateway=gateway, **options)
        ifdown(network)

    # For VLAN we should attach bridge only to the VLAN device
    # rather than to underlying NICs or bond
    brName = network if bridged else None
    bridgeForNic = None if vlan else brName

    # NICs must be activated in the same order of boot time
    # to expose the correct MAC address.
    for nic in nicSort(nics):
        configWriter.addNic(nic, bonding=bonding, bridge=bridgeForNic,
                             mtu=max(prevmtu, mtu))
        ifup(nic)
    if bonding:
        configWriter.addBonding(bonding, bridge=bridgeForNic,
                                 bondingOptions=bondingOptions,
                                 mtu=max(prevmtu, mtu))
        ifup(bonding)

    if vlan:
        configWriter.addVlan(vlan, iface, network=brName,
                             mtu=mtu, bridged=bridged)
        iface += '.' + vlan
        ifup(iface)

    if bridged:
        if options.get('bootproto') == 'dhcp' and not utils.tobool(options.get('blockingdhcp')):
            # wait for dhcp in another thread, so vdsm won't get stuck (BZ#498940)
            t = threading.Thread(target=ifup, name='ifup-waiting-on-dhcp', args=(network,))
            t.daemon = True
            t.start()
        else:
            ifup(network)

    # add libvirt network
    createLibvirtNetwork(network, bridged, iface)

def createLibvirtNetwork(network, bridged=True, iface=None):
    conn = libvirtconnection.get()
    netName = LIBVIRT_NET_PREFIX + network
    if bridged:
        netXml = '''<network><name>%s</name><forward mode='bridge'/>
                    <bridge name='%s'/></network>''' % (escape(netName), escape(network))
    else:
        netXml = '''<network><name>%s</name><forward mode='passthrough'>
                    <interface dev='%s'/></forward></network>''' % (escape(netName), escape(iface))
    removeLibvirtNetwork(network, log=False)
    net = conn.networkDefineXML(netXml)
    net.create()
    net.setAutostart(1)

def removeLibvirtNetwork(network, log=True):
    netName = LIBVIRT_NET_PREFIX + network
    conn = libvirtconnection.get()
    try:
        net = conn.networkLookupByName(netName)
        if net.isActive():
            net.destroy()
        if net.isPersistent():
            net.undefine()
    except libvirt.libvirtError:
        if log:
            logging.debug('failed to remove libvirt network %s', netName,
                          exc_info=True)

def assertBridgeClean(bridge, vlan, bonding, nics):
    brifs = os.listdir('/sys/class/net/%s/brif/' % bridge)
    for nic in nics:
        try:
            brifs.remove(nic)
        except:
            pass
    if vlan:
        brif = (bonding or nics[0]) + '.' + vlan
    else:
        brif = bonding
    try:
        brifs.remove(brif)
    except:
        pass

    if brifs:
        raise ConfigNetworkError(ne.ERR_USED_BRIDGE, 'bridge %s has interfaces %s connected' % (bridge, brifs))

def showNetwork(network):
    _netinfo = NetInfo()
    if network not in _netinfo.networks:
        print "Network %r doesn't exist" % network
        return

    bridged = _netinfo.networks[network]['bridged']
    print "Network %s(Bridged: %s):" % (network, bridged)

    nics, vlan, bonding = _netinfo.getNicsVlanAndBondingForNetwork(network)

    if bridged:
        ipaddr = _netinfo.networks[network]['addr']
        netmask = _netinfo.networks[network]['netmask']
        gateway = _netinfo.networks[network]['gateway']
        print "ipaddr=%s, netmask=%s, gateway=%s" % (ipaddr, netmask, gateway)
    else:
        iface = _netinfo.networks[network]['interface']
        ipaddr = _netinfo.nics[iface]['addr']
        netmask = _netinfo.nics[iface]['netmask']
        print "ipaddr=%s, netmask=%s" % (ipaddr, netmask)

    print "vlan=%s, bonding=%s, nics=%s" % (vlan, bonding, nics)

def listNetworks():
    _netinfo = NetInfo()
    print "Networks:", _netinfo.networks.keys()
    print "Vlans:", _netinfo.vlans.keys()
    print "Nics:", _netinfo.nics.keys()
    print "Bondings:", _netinfo.bondings.keys()

def delNetwork(network, vlan=None, bonding=None, nics=None, force=False,
               configWriter=None, implicitBonding=True, **options):
    _netinfo = NetInfo()

    validateBridgeName(network)

    if network not in _netinfo.networks:
        raise ConfigNetworkError(ne.ERR_BAD_BRIDGE,
                "Cannot delete network %r: It doesn't exist" % network)

    nics, vlan, bonding = _netinfo.getNicsVlanAndBondingForNetwork(network)
    bridged = _netinfo.networks[network]['bridged']

    logging.info("Removing network %s with vlan=%s, bonding=%s, nics=%s,"
                 "options=%s" % (network, vlan, bonding, nics, options))

    if not utils.tobool(force):
        if bonding:
            validateBondingName(bonding)
            if set(nics) != set(_netinfo.bondings[bonding]["slaves"]):
                raise ConfigNetworkError(ne.ERR_BAD_NIC,
                        "delNetwork: %s are not all nics enslaved to %s" % \
                        (nics, bonding))
        if vlan:
            validateVlanId(vlan)
        if bridged:
            assertBridgeClean(network, vlan, bonding, nics)

    if configWriter is None:
        configWriter = ConfigWriter()

    if bridged:
        configWriter.setNewMtu(network)

    removeLibvirtNetwork(network, log=False)
    # We need to gather NetInfo again to refresh networks info from libvirt.
    # The deleted bridge should never be up at this stage.
    if network in NetInfo().networks:
        raise ConfigNetworkError(ne.ERR_USED_BRIDGE,
                "delNetwork: bridge %s still exists" % network)

    if network and bridged:
        ifdown(network)
        subprocess.call([constants.EXT_BRCTL, 'delbr', network])
        configWriter.removeBridge(network)

    if vlan:
        vlandev = (bonding or nics[0]) + '.' + vlan
        ifdown(vlandev)
        subprocess.call([constants.EXT_VCONFIG, 'rem', vlandev],
                        stderr=subprocess.PIPE)
        configWriter.removeVlan(vlan, bonding or nics[0])

    # The (relatively) new setupNetwork verb allows to remove a network
    # defined on top of an bonding device without break the bond itself.
    if implicitBonding:
        if bonding:
            if not bridged or not bondingOtherUsers(network, vlan, bonding):
                ifdown(bonding)
                configWriter.removeBonding(bonding)

        for nic in nics:
            nicUsers = nicOtherUsers(network, vlan, bonding, nic)
            if not nicUsers:
                ifdown(nic)
                configWriter.removeNic(nic)

def clientSeen(timeout):
    start = time.time()
    while timeout >= 0:
        if os.stat(constants.P_VDSM_CLIENT_LOG).st_mtime > start:
            return True
        time.sleep(1)
        timeout -= 1
    return False


def editNetwork(oldBridge, newBridge, vlan=None, bonding=None, nics=None, **options):
    configWriter = ConfigWriter()
    try:
        delNetwork(oldBridge, configWriter=configWriter, **options)
        addNetwork(newBridge, vlan=vlan, bonding=bonding, nics=nics, configWriter=configWriter, **options)
    except:
        configWriter.restoreAtomicBackup()
        raise
    if utils.tobool(options.get('connectivityCheck', False)):
        if not clientSeen(int(options.get('connectivityTimeout', CONNECTIVITY_TIMEOUT_DEFAULT))):
            delNetwork(newBridge, force=True)
            configWriter.restoreAtomicBackup()
            return define.errCode['noConPeer']['status']['code']

def _validateNetworkSetup(networks={}, bondings={}, explicitBonding=False):
    _netinfo = NetInfo()

    # Step 1: Initial validation (validate names, existence of params, etc.)
    for network, networkAttrs in networks.iteritems():
        validateBridgeName(network)

        if networkAttrs.get('remove', False):
            if set(networkAttrs) - set(['remove']):
                raise ConfigNetworkError(ne.ERR_BAD_PARAMS, "Cannot specify any attribute when removing")
            if network not in _netinfo.networks:
                raise ConfigNetworkError(ne.ERR_BAD_BRIDGE, 'Cannot remove bridge %s: Doesn\'t exist' % network)
            continue

        vlan = networkAttrs.get('vlan', None)
        ipaddr = networkAttrs.get('ipaddr', None)
        netmask = networkAttrs.get('netmask', None)
        gateway = networkAttrs.get('gateway', None)
        if vlan:
            validateVlanId(vlan)

        # Check ip, netmask, gateway
        if ipaddr:
            if not netmask:
                raise ConfigNetworkError(ne.ERR_BAD_ADDR, "Must specify netmask to configure ip for bridge")
            validateIpAddress(ipaddr)
            validateNetmask(netmask)
            if gateway:
                validateGateway(gateway)
        else:
            if netmask or gateway:
                raise ConfigNetworkError(ne.ERR_BAD_ADDR, "Specified netmask or gateway but not ip")

        # check nic or bonding
        nic = networkAttrs.get('nic', None)
        bonding = networkAttrs.get('bonding', None)

        if nic and bonding:
            raise ConfigNetworkError(ne.ERR_BAD_PARAMS, "Don't specify both nic and bonding")
        if not nic and not bonding:
            raise ConfigNetworkError(ne.ERR_BAD_PARAMS, "Must specify either nic or bonding")

        if nic and nic not in _netinfo.nics:
            raise ConfigNetworkError(ne.ERR_BAD_NIC, "unknown nic: %r"%nic)

    for bonding, bondingAttrs in bondings.iteritems():
        validateBondingName(bonding)
        if 'options' in bondingAttrs:
            validateBondingOptions(bonding, bondingAttrs['options'])

        if bondingAttrs.get('remove', False):
            if bonding not in _netinfo.bondings:
                raise ConfigNetworkError(ne.ERR_BAD_BONDING, 'Cannot remove bonding %s: Doesn\'t exist' % bonding)
            continue

        nics = bondingAttrs.get('nics', None)
        if not nics:
            raise ConfigNetworkError(ne.ERR_BAD_PARAMS, "Must specify nics for bonding")
        if not set(nics).issubset(set(_netinfo.nics)):
            raise ConfigNetworkError(ne.ERR_BAD_NIC, "Unknown nics in: %r"%list(nics))


    # Step 2: Make sure we have complete information about the Setup, more validation
    # (if explicitBonding==False we complete the missing information ourselves, else we raise an exception)
    nics = defaultdict(lambda: {'networks':{}, 'bonding':None})
    for network, networkAttrs in networks.iteritems():
        if networkAttrs.get('remove', False):
            continue

        if 'bonding' in networkAttrs:
            assert 'nic' not in networkAttrs

            bonding = networkAttrs['bonding']
            if bonding not in bondings:
                if explicitBonding:
                    raise ConfigNetworkError(ne.ERR_BAD_PARAMS, "Network %s requires unspecified bonding %s"%(
                                             network, bonding))

                # fill in bonding info
                bondings[bonding] =  {'nics':_netinfo.bondings[bonding]['slaves']}

            if '_networks' not in bondings[bonding]:
                bondings[bonding]['_networks'] = {}
            bondings[bonding]['_networks'][network] = networkAttrs
        else:
            assert 'nic' in networkAttrs

            nics[networkAttrs['nic']]['networks'][network] = networkAttrs

    for bonding, bondingAttrs in bondings.iteritems():
        if bondingAttrs.get('remove', False):
            continue
        connectedNetworks = _netinfo.getNetworksForNic(bonding)

        for network in connectedNetworks:
            if network not in networks:
                if explicitBonding:
                    raise ConfigNetworkError(ne.ERR_BAD_PARAMS, "Bonding %s is associated with unspecified network %s"%(
                                             bonding, network))
                # fill in network info
                _, vlan, bonding2 = _netinfo.getNicsVlanAndBondingForNetwork(network)
                assert bonding == bonding2
                networks[network] = {'bonding': bonding, 'vlan':vlan}

        for nic in bondingAttrs['nics']:
            if nics[nic]['bonding']:
                raise ConfigNetworkError(ne.ERR_BAD_BONDING, "Nic %s is attached to two different bondings in setup: %s, %s"%(
                                         nic, bonding, nics[nic]['bonding']))
            nics[nic]['bonding'] = bonding

    # At this point the state may be contradictory.

    # Step 3: Apply removals (We're not iterating because we change the dictionary size)
    queue = []
    for network, networkAttrs in networks.items():
        if networkAttrs.get('remove', False):
            del networks[network]
        else:
            queue.append(('network', network, networkAttrs))
    for bonding, bondingAttrs in bondings.items():
        if bondingAttrs.get('remove', False):
            del bondings[bonding]
        else:
            queue.append(('bonding', bonding, bondingAttrs))

    # Step 4: Verify Setup
    for nic, nicAttrs in nics.iteritems():
        networks = nicAttrs['networks']
        if networks and nicAttrs['bonding']:
            raise ConfigNetworkError(ne.ERR_USED_NIC, "Setup attached both network and bonding to nic %s"%(nic))
        if len(networks) > 1:
            for network, networkAttrs in networks.iteritems():
                if not networkAttrs.get('vlan', None):
                    raise ConfigNetworkError(ne.ERR_USED_NIC,
                            "Setup attached more than one network to nic %s, some of which aren't vlans"%(nic))

    for bonding, bondingAttrs in bondings.iteritems():
        networks = bondingAttrs.get('_networks', {})
        if len(networks) > 1:
            for network, networkAttrs in networks.iteritems():
                if not networkAttrs.get('vlan', None):
                    raise ConfigNetworkError(ne.ERR_BAD_BONDING,
                            "Setup attached more than one network to bonding %s, some of which aren't vlans"%(bonding))


def _editBondings(bondings, configWriter):
    """ Add/Edit bond interface """
    logger = logging.getLogger("_editBondings")

    _netinfo = NetInfo()

    for bond, bondAttrs in bondings.iteritems():
        logger.debug("Creating/Editing bond %s with attributes %s",
                        bond, bondAttrs)
        if bond in _netinfo.bondings:
            ifdown(bond)
            # Take down all bond's NICs.
            for nic in _netinfo.getNicsForBonding(bond):
                ifdown(nic)
                configWriter.removeNic(nic)

        # NICs must be activated in the same order of boot time
        # to expose the correct MAC address.
        for nic in nicSort(bondAttrs['nics']):
            configWriter.addNic(nic, bonding=bond)
            ifup(nic)

        configWriter.addBonding(bond,
                                bondingOptions=bondAttrs.get('options', None))
        ifup(bond)

def _removeBondings(bondings, configWriter):
    """ Add/Edit bond interface """
    logger = logging.getLogger("_removeBondings")

    _netinfo = NetInfo()

    for bond, bondAttrs in bondings.items():
        if 'remove' in bondAttrs:
            nics = _netinfo.getNicsForBonding(bond)
            logger.debug("Removing bond %r with nics = %s", bond, nics)
            ifdown(bond)
            configWriter.removeBonding(bond)

            for nic in nics:
                ifdown(nic)
                configWriter.removeNic(nic)

            del bondings[bond]


def setupNetworks(networks={}, bondings={}, **options):
    """Add/Edit/Remove configuration for networks and bondings.

    Params:
        networks - dict of key=network, value=attributes
                   where 'attributes' is a dict with the following optional items:
                        vlan=<id>
                        bonding="<name>" | nic="<name>"
                        (bonding and nics are mutually exclusive)
                        ipaddr="<ip>"
                        netmask="<ip>"
                        gateway="<ip>"
                        bootproto="..."
                        delay="..."
                        onboot="yes"|"no"
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
                        explicitBonding=0|1


    Notes:
        Bondings are removed when they change state from 'used' to 'unused'.

        By default, if you edit a network that is attached to a bonding, it's not
        necessary to re-specify the bonding (you need only to note the attachment
        in the network's attributes). Similarly, if you edit a bonding, it's not
        necessary to specify its networks.
        However, if you specify the 'explicitBonding' option as true, the function
        will expect you to specify all networks that are attached to a specified
        bonding, and vice-versa, the bonding attached to a specified network.

    """
    logger = logging.getLogger("setupNetworks")

    try:
        _netinfo = NetInfo()
        configWriter = ConfigWriter()
        networksAdded = set()
        # keep set netsWithNewBonds to be able remove
        # a new added network if connectivity check fail.
        # If a new network needs to be created on top of existing bond,
        # we will need to keep the bond on rollback flow,
        # else we will break the new created bond.
        netsWithNewBonds = set()

        logger.debug("Setting up network according to configuration: "
                     "networks:%r, bondings:%r, options:%r" % (networks,
                     bondings, options))

        force = options.get('force', False)
        if not utils.tobool(force):
            logging.debug("Validating configuration")
            _validateNetworkSetup(dict(networks), dict(bondings),
                                  explicitBonding=options.get('explicitBonding',
                                                              False))

        logger.debug("Applying...")
        try:
            # Remove edited networks and networks with 'remove' attribute
            for network, networkAttrs in networks.items():
                if network in _netinfo.networks:
                    logger.debug("Removing network %r" % network)
                    delNetwork(network, configWriter=configWriter, force=force,
                               implicitBonding=False)
                    if 'remove' in networkAttrs:
                        del networks[network]
                else:
                    networksAdded.add(network)

            handledBonds = set()
            for network, networkAttrs in networks.iteritems():
                d = dict(networkAttrs)
                if 'bonding' in d:
                    # we may not receive any information
                    # about the bonding device if it is unchanged
                    if bondings.get(d['bonding']):
                        d['nics'] = bondings[d['bonding']]['nics']
                        d['bondingOptions'] = bondings[d['bonding']].get('options',
                                                                         None)
                        # Don't remove bondX from the bonding list here,
                        # because it may be in use for other networks
                        handledBonds.add(d['bonding'])
                        # we create a new bond
                        if network in networksAdded:
                            netsWithNewBonds.add(network)
                else:
                    d['nics'] = [d.pop('nic')]
                d['force'] = force

                logger.debug("Adding network %r" % network)
                addNetwork(network, configWriter=configWriter,
                           implicitBonding=True, **d)

            # Do not handle a bonding device twice.
            # We already handled it before during addNetwork.
            for bond in handledBonds:
                del bondings[bond]

            # We are now left with bondings whose network was not mentioned
            # Remove bonds with 'remove' attribute
            _removeBondings(bondings, configWriter)

            # Check whether bonds should be resized
            _editBondings(bondings, configWriter)

            if utils.tobool(options.get('connectivityCheck', True)):
                logger.debug('Checking connectivity...')
                if not clientSeen(int(options.get('connectivityTimeout',
                                      CONNECTIVITY_TIMEOUT_DEFAULT))):
                    logger.info('Connectivity check failed, rolling back')
                    for network in networksAdded:
                        delNetwork(network, force=True,
                                   implicitBonding=network in netsWithNewBonds)
                    raise ConfigNetworkError(ne.ERR_LOST_CONNECTION,
                                             'connectivity check failed')
        except:
            configWriter.restoreAtomicBackup()
            raise

    except Exception, e:
        # SuperVdsm eats the error, so let's print it ourselves
        logger.error(e, exc_info=True)
        raise

def setSafeNetworkConfig():
    """Declare current network configuration as 'safe'"""
    subprocess.Popen([constants.EXT_VDSM_STORE_NET_CONFIG])

def usage():
    print """Usage:
    ./configNetwork.py add Network <attributes> <options>
                       edit oldNetwork newNetwork <attributes> <options>
                       del Network <options>
                       setup Network [None|attributes] [++ Network [None|attributes] [++ ...]] [:: <options>]

                       attributes = [vlan=...] [bonding=...] [nics=<nic1>,...]
                       options = [Force=<True|False>] [bridged=<True|False>]...
    """


def _parseKwargs(args):
    return dict(arg.split('=', 1) for arg in args)

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
        addNetwork(bridge, **kwargs)
    elif sys.argv[1] == 'del':
        bridge = sys.argv[2]
        kwargs = _parseKwargs(sys.argv[3:])
        if 'nics' in kwargs:
            kwargs['nics'] = kwargs['nics'].split(',')
        delNetwork(bridge, **kwargs)
    elif sys.argv[1] == 'edit':
        oldBridge = sys.argv[2]
        newBridge = sys.argv[3]
        kwargs = _parseKwargs(sys.argv[4:])
        if 'nics' in kwargs:
            kwargs['nics'] = kwargs['nics'].split(',')
        editNetwork(oldBridge, newBridge, **kwargs)
    elif sys.argv[1] == 'setup':
        batchCommands, options = utils.listSplit( sys.argv[2:], '::', 1 )
        d = {}
        for batchCommand in utils.listSplit( batchCommands, '++' ):
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
    except ConfigNetworkError, e:
        traceback.print_exc()
        print e.message
        sys.exit(e.errCode)
    sys.exit(0)
