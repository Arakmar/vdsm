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

from operator import itemgetter
import xml.dom
import xml.dom.minidom
import xml.etree.ElementTree as etree

from vdsm import constants
from vdsm import utils

import caps

METADATA_VM_TUNE_URI = 'http://ovirt.org/vm/tune/1.0'
METADATA_VM_TUNE_ELEMENT = 'qos'
METADATA_VM_TUNE_PREFIX = 'ovirt'


def has_channel(domXML, name):
    domObj = etree.fromstring(domXML)
    devices = domObj.findall('devices')

    if len(devices) == 1:
        for chan in devices[0].findall('channel'):
            targets = chan.findall('target')
            if len(targets) == 1:
                if targets[0].attrib['name'] == name:
                    return True

    return False


def all_devices(domXML):
    domObj = xml.dom.minidom.parseString(domXML)
    devices = domObj.childNodes[0].getElementsByTagName('devices')[0]

    for deviceXML in devices.childNodes:
        if deviceXML.nodeType == xml.dom.Node.ELEMENT_NODE:
            yield deviceXML


def filter_devices_with_alias(devices):
    for deviceXML in devices:
        aliasElement = deviceXML.getElementsByTagName('alias')
        if aliasElement:
            alias = aliasElement[0].getAttribute('name')
            yield deviceXML, alias


class Device(object):
    # since we're inheriting all VM devices from this class, __slots__ must
    # be initialized here in order to avoid __dict__ creation
    __slots__ = ()

    def createXmlElem(self, elemType, deviceType, attributes=()):
        """
        Create domxml device element according to passed in params
        """
        elemAttrs = {}
        element = Element(elemType)

        if deviceType:
            elemAttrs['type'] = deviceType

        for attrName in attributes:
            if not hasattr(self, attrName):
                continue

            attr = getattr(self, attrName)
            if isinstance(attr, dict):
                element.appendChildWithArgs(attrName, **attr)
            else:
                elemAttrs[attrName] = attr

        element.setAttrs(**elemAttrs)
        return element


class Element(object):

    def __init__(self, tagName, text=None, namespaceUri=None, **attrs):
        if namespaceUri is not None:
            self._elem = xml.dom.minidom.Document().createElementNS(
                namespaceUri, tagName)
        else:
            self._elem = xml.dom.minidom.Document().createElement(tagName)
        self.setAttrs(**attrs)
        if text is not None:
            self.appendTextNode(text)

    def __getattr__(self, name):
        return getattr(self._elem, name)

    def setAttrs(self, **attrs):
        for attrName, attrValue in attrs.iteritems():
            self._elem.setAttribute(attrName, attrValue)

    def setAttr(self, attrName, attrValue):
        self._elem.setAttribute(attrName, attrValue)

    def appendTextNode(self, text):
        textNode = xml.dom.minidom.Document().createTextNode(text)
        self._elem.appendChild(textNode)

    def appendChild(self, element):
        self._elem.appendChild(element)

    def appendChildWithArgs(self, childName, text=None, **attrs):
        child = Element(childName, text, **attrs)
        self._elem.appendChild(child)
        return child


class Domain(object):

    def __init__(self, conf, log, arch):
        """
        Create the skeleton of a libvirt domain xml

        <domain type="kvm">
            <name>vmName</name>
            <uuid>9ffe28b6-6134-4b1e-8804-1185f49c436f</uuid>
            <memory>262144</memory>
            <currentMemory>262144</currentMemory>
            <vcpu current='smp'>160</vcpu>
            <devices>
            </devices>
        </domain>

        """
        self.conf = conf
        self.log = log

        self.arch = arch

        self.doc = xml.dom.minidom.Document()

        if utils.tobool(self.conf.get('kvmEnable', 'true')):
            domainType = 'kvm'
        else:
            domainType = 'qemu'

        domainAttrs = {'type': domainType}

        self.dom = Element('domain', **domainAttrs)
        self.doc.appendChild(self.dom)

        self.dom.appendChildWithArgs('name', text=self.conf['vmName'])
        self.dom.appendChildWithArgs('uuid', text=self.conf['vmId'])
        if 'numOfIoThreads' in self.conf:
            self.dom.appendChildWithArgs('iothreads',
                                         text=str(self.conf['numOfIoThreads']))
        memSizeKB = str(int(self.conf.get('memSize', '256')) * 1024)
        self.dom.appendChildWithArgs('memory', text=memSizeKB)
        self.dom.appendChildWithArgs('currentMemory', text=memSizeKB)
        if 'maxMemSize' in self.conf:
            maxMemSizeKB = str(int(self.conf['maxMemSize']) * 1024)
            maxMemSlots = str(self.conf.get('maxMemSlots', '16'))
            self.dom.appendChildWithArgs('maxMemory', text=maxMemSizeKB,
                                         slots=maxMemSlots)
        vcpu = self.dom.appendChildWithArgs('vcpu', text=self._getMaxVCpus())
        vcpu.setAttrs(**{'current': self._getSmp()})

        self._devices = Element('devices')
        self.dom.appendChild(self._devices)

        self.appendMetadata()

    def appendClock(self):
        """
        Add <clock> element to domain:

        <clock offset="variable" adjustment="-3600">
            <timer name="rtc" tickpolicy="catchup">
        </clock>

        for hyperv:
        <clock offset="variable" adjustment="-3600">
            <timer name="hypervclock" present="yes">
            <timer name="rtc" tickpolicy="catchup">
        </clock>
        """

        m = Element('clock', offset='variable',
                    adjustment=str(self.conf.get('timeOffset', 0)))
        if utils.tobool(self.conf.get('hypervEnable', 'false')):
            m.appendChildWithArgs('timer', name='hypervclock', present='yes')
        m.appendChildWithArgs('timer', name='rtc', tickpolicy='catchup')
        m.appendChildWithArgs('timer', name='pit', tickpolicy='delay')

        if self.arch == caps.Architecture.X86_64:
            m.appendChildWithArgs('timer', name='hpet', present='no')

        self.dom.appendChild(m)

    def appendMetadata(self):
        """
        Add the namespaced qos metadata element to the domain

        <domain xmlns:ovirt="http://ovirt.org/vm/tune/1.0">
        ...
           <metadata>
              <ovirt:qos xmlns:ovirt=>
           </metadata>
        ...
        </domain>
        """

        self._metadata = Element('metadata')
        self._metadata.appendChild(Element(METADATA_VM_TUNE_PREFIX + ':' +
                                           METADATA_VM_TUNE_ELEMENT,
                                           namespaceUri=METADATA_VM_TUNE_URI))
        self.dom.setAttr('xmlns:' + METADATA_VM_TUNE_PREFIX,
                         METADATA_VM_TUNE_URI)
        self.dom.appendChild(self._metadata)

    def appendOs(self):
        """
        Add <os> element to domain:

        <os>
            <type arch="x86_64" machine="pc">hvm</type>
            <boot dev="cdrom"/>
            <kernel>/tmp/vmlinuz-2.6.18</kernel>
            <initrd>/tmp/initrd-2.6.18.img</initrd>
            <cmdline>ARGs 1</cmdline>
            <smbios mode="sysinfo"/>
        </os>
        """

        oselem = Element('os')
        self.dom.appendChild(oselem)

        DEFAULT_MACHINES = {caps.Architecture.X86_64: 'pc',
                            caps.Architecture.PPC64: 'pseries',
                            caps.Architecture.PPC64LE: 'pseries'}

        machine = self.conf.get('emulatedMachine', DEFAULT_MACHINES[self.arch])

        oselem.appendChildWithArgs('type', text='hvm', arch=self.arch,
                                   machine=machine)

        qemu2libvirtBoot = {'a': 'fd', 'c': 'hd', 'd': 'cdrom', 'n': 'network'}
        for c in self.conf.get('boot', ''):
            oselem.appendChildWithArgs('boot', dev=qemu2libvirtBoot[c])

        if self.conf.get('initrd'):
            oselem.appendChildWithArgs('initrd', text=self.conf['initrd'])

        if self.conf.get('kernel'):
            oselem.appendChildWithArgs('kernel', text=self.conf['kernel'])

        if self.conf.get('kernelArgs'):
            oselem.appendChildWithArgs('cmdline', text=self.conf['kernelArgs'])

        if self.arch == caps.Architecture.X86_64:
            oselem.appendChildWithArgs('smbios', mode='sysinfo')

        if utils.tobool(self.conf.get('bootMenuEnable', False)):
            oselem.appendChildWithArgs('bootmenu', enable='yes')

    def appendSysinfo(self, osname, osversion, serialNumber):
        """
        Add <sysinfo> element to domain:

        <sysinfo type="smbios">
          <bios>
            <entry name="vendor">QEmu/KVM</entry>
            <entry name="version">0.13</entry>
          </bios>
          <system>
            <entry name="manufacturer">Fedora</entry>
            <entry name="product">Virt-Manager</entry>
            <entry name="version">0.8.2-3.fc14</entry>
            <entry name="serial">32dfcb37-5af1-552b-357c-be8c3aa38310</entry>
            <entry name="uuid">c7a5fdbd-edaf-9455-926a-d65c16db1809</entry>
          </system>
        </sysinfo>
        """

        sysinfoelem = Element('sysinfo', type='smbios')
        self.dom.appendChild(sysinfoelem)

        syselem = Element('system')
        sysinfoelem.appendChild(syselem)

        def appendEntry(k, v):
            syselem.appendChildWithArgs('entry', text=v, name=k)

        appendEntry('manufacturer', constants.SMBIOS_MANUFACTURER)
        appendEntry('product', osname)
        appendEntry('version', osversion)
        appendEntry('serial', serialNumber)
        appendEntry('uuid', self.conf['vmId'])

    def appendFeatures(self):
        """
        Add machine features to domain xml.

        Currently only
        <features>
            <acpi/>
        <features/>

        for hyperv:
        <features>
            <acpi/>
            <hyperv>
                <relaxed state='on'/>
            </hyperv>
        <features/>
        """

        if (utils.tobool(self.conf.get('acpiEnable', 'true')) or
           utils.tobool(self.conf.get('hypervEnable', 'false'))):
            features = self.dom.appendChildWithArgs('features')

        if utils.tobool(self.conf.get('acpiEnable', 'true')):
            features.appendChildWithArgs('acpi')

        if utils.tobool(self.conf.get('hypervEnable', 'false')):
            hyperv = Element('hyperv')
            features.appendChild(hyperv)

            hyperv.appendChildWithArgs('relaxed', state='on')
            # turns off an internal Windows watchdog, and by doing so avoids
            # some high load BSODs.
            hyperv.appendChildWithArgs('vapic', state='on')
            # magic number taken from recomendations. References:
            # https://bugzilla.redhat.com/show_bug.cgi?id=1083529#c10
            # https://bugzilla.redhat.com/show_bug.cgi?id=1053846#c0
            hyperv.appendChildWithArgs(
                'spinlocks', state='on', retries='8191')

    def appendCpu(self):
        """
        Add guest CPU definition.

        <cpu match="exact">
            <model>qemu64</model>
            <topology sockets="S" cores="C" threads="T"/>
            <feature policy="require" name="sse2"/>
            <feature policy="disable" name="svm"/>
        </cpu>

        For POWER8, there is no point in trying to use baseline CPU for flags
        since there are only HW features. There are 2 ways of creating a valid
        POWER8 element that we support:

            <cpu>
                <model>POWER{X}</model>
            </cpu>

        This translates to -cpu POWER{X} (where {X} is version of the
        processor - 7 and 8), which tells qemu to emulate the CPU in POWER8
        family that it's capable of emulating - in case of hardware
        virtualization, that will be the host cpu (so an equivalent of
        -cpu host). Using this option does not limit migration between POWER8
        machines - it is still possible to migrate from e.g. POWER8 to
        POWER8e. The second option is not supported and serves only for
        reference:

            <cpu mode="host-model">
                <model>power{X}</model>
            </cpu>

        where {X} is the binary compatibility version of POWER that we
        require (6, 7, 8). This translates to qemu's -cpu host,compat=power{X}.

        Using the second option also does not limit migration between POWER8
        machines - it is still possible to migrate from e.g. POWER8 to POWER8e.
        """

        cpu = Element('cpu')

        if self.arch in (caps.Architecture.X86_64,):
            cpu.setAttrs(match='exact')

            features = self.conf.get('cpuType', 'qemu64').split(',')
            model = features[0]

            if model == 'hostPassthrough':
                cpu.setAttrs(mode='host-passthrough')
            elif model == 'hostModel':
                cpu.setAttrs(mode='host-model')
            else:
                cpu.appendChildWithArgs('model', text=model)

                # This hack is for backward compatibility as the libvirt
                # does not allow 'qemu64' guest on intel hardware
                if model == 'qemu64' and '+svm' not in features:
                    features += ['-svm']

                for feature in features[1:]:
                    # convert Linux name of feature to libvirt
                    if feature[1:6] == 'sse4_':
                        feature = feature[0] + 'sse4.' + feature[6:]

                    featureAttrs = {'name': feature[1:]}
                    if feature[0] == '+':
                        featureAttrs['policy'] = 'require'
                    elif feature[0] == '-':
                        featureAttrs['policy'] = 'disable'
                    cpu.appendChildWithArgs('feature', **featureAttrs)
        elif self.arch in caps.Architecture.POWER:
            features = self.conf.get('cpuType', 'POWER8').split(',')
            model = features[0]
            cpu.appendChildWithArgs('model', text=model)

        if ('smpCoresPerSocket' in self.conf or
                'smpThreadsPerCore' in self.conf):
            maxVCpus = int(self._getMaxVCpus())
            cores = int(self.conf.get('smpCoresPerSocket', '1'))
            threads = int(self.conf.get('smpThreadsPerCore', '1'))
            cpu.appendChildWithArgs('topology',
                                    sockets=str(maxVCpus / cores / threads),
                                    cores=str(cores), threads=str(threads))

        # CPU-pinning support
        # see http://www.ovirt.org/wiki/Features/Design/cpu-pinning
        if 'cpuPinning' in self.conf:
            cputune = Element('cputune')
            cpuPinning = self.conf.get('cpuPinning')
            for cpuPin in cpuPinning.keys():
                cputune.appendChildWithArgs('vcpupin', vcpu=cpuPin,
                                            cpuset=cpuPinning[cpuPin])
            self.dom.appendChild(cputune)

        # Guest numa topology support
        # see http://www.ovirt.org/Features/NUMA_and_Virtual_NUMA
        if 'guestNumaNodes' in self.conf:
            numa = Element('numa')
            guestNumaNodes = sorted(
                self.conf.get('guestNumaNodes'), key=itemgetter('nodeIndex'))
            for vmCell in guestNumaNodes:
                nodeMem = int(vmCell['memory']) * 1024
                numa.appendChildWithArgs('cell',
                                         cpus=vmCell['cpus'],
                                         memory=str(nodeMem))
            cpu.appendChild(numa)

        self.dom.appendChild(cpu)

    # Guest numatune support
    def appendNumaTune(self):
        """
        Add guest numatune definition.

        <numatune>
            <memory mode='strict' nodeset='0-1'/>
        </numatune>
        """

        if 'numaTune' in self.conf:
            numaTune = self.conf.get('numaTune')
            if 'nodeset' in numaTune.keys():
                mode = numaTune.get('mode', 'strict')
                numatune = Element('numatune')
                numatune.appendChildWithArgs('memory', mode=mode,
                                             nodeset=numaTune['nodeset'])
                self.dom.appendChild(numatune)

    def _appendAgentDevice(self, path, name):
        """
          <channel type='unix'>
             <target type='virtio' name='org.linux-kvm.port.0'/>
             <source mode='bind' path='/tmp/socket'/>
          </channel>
        """
        channel = Element('channel', type='unix')
        channel.appendChildWithArgs('target', type='virtio', name=name)
        channel.appendChildWithArgs('source', mode='bind', path=path)
        self._devices.appendChild(channel)

    def appendInput(self):
        """
        Add input device.

        <input bus="ps2" type="mouse"/>
        """
        if utils.tobool(self.conf.get('tabletEnable')):
            inputAttrs = {'type': 'tablet', 'bus': 'usb'}
        elif self.arch == caps.Architecture.X86_64:
            inputAttrs = {'type': 'mouse', 'bus': 'ps2'}
        else:
            inputAttrs = {'type': 'mouse', 'bus': 'usb'}

        self._devices.appendChildWithArgs('input', **inputAttrs)

    def appendEmulator(self):
        emulatorPath = '/usr/bin/qemu-system-' + self.arch

        emulator = Element('emulator', text=emulatorPath)

        self._devices.appendChild(emulator)

    def appendDeviceXML(self, deviceXML):
        self._devices.appendChild(
            xml.dom.minidom.parseString(deviceXML).firstChild)

    def toxml(self):
        return self.doc.toprettyxml(encoding='utf-8')

    def _getSmp(self):
        return self.conf.get('smp', '1')

    def _getMaxVCpus(self):
        return self.conf.get('maxVCpus', self._getSmp())
