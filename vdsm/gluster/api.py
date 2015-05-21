#
# Copyright 2012 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from functools import wraps
from vdsm.define import doneCode

import supervdsm as svdsm
import exception as ge

_SUCCESS = {'status': doneCode}

GLUSTER_RPM_PACKAGES = (
    ('glusterfs', ('glusterfs',)),
    ('glusterfs-fuse', ('glusterfs-fuse',)),
    ('glusterfs-geo-replication', ('glusterfs-geo-replication',)),
    ('glusterfs-rdma', ('glusterfs-rdma',)),
    ('glusterfs-server', ('glusterfs-server',)),
    ('gluster-swift', ('gluster-swift',)),
    ('gluster-swift-account', ('gluster-swift-account',)),
    ('gluster-swift-container', ('gluster-swift-container',)),
    ('gluster-swift-doc', ('gluster-swift-doc',)),
    ('gluster-swift-object', ('gluster-swift-object',)),
    ('gluster-swift-proxy', ('gluster-swift-proxy',)),
    ('gluster-swift-plugin', ('gluster-swift-plugin',)))

GLUSTER_DEB_PACKAGES = (
    ('glusterfs', 'glusterfs-client'),
    ('glusterfs-fuse', 'libglusterfs0'),
    ('glusterfs-geo-replication', 'libglusterfs0'),
    ('glusterfs-rdma', 'libglusterfs0'),
    ('glusterfs-server', 'glusterfs-server'))


def exportAsVerb(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        rv = func(*args, **kwargs)
        if rv:
            rv.update(_SUCCESS)
            return rv
        else:
            return _SUCCESS

    wrapper.exportAsVerb = True
    return wrapper


class GlusterApi(object):
    """
    The gluster interface of vdsm.

    """

    def __init__(self, cif, log):
        self.cif = cif
        self.log = log
        self.svdsmProxy = svdsm.getProxy()

    @exportAsVerb
    def volumesList(self, volumeName=None, remoteServer=None, options=None):
        return {'volumes': self.svdsmProxy.glusterVolumeInfo(volumeName,
                                                             remoteServer)}

    @exportAsVerb
    def volumeCreate(self, volumeName, brickList, replicaCount=0,
                     stripeCount=0, transportList=[],
                     force=False, options=None):
        return self.svdsmProxy.glusterVolumeCreate(volumeName, brickList,
                                                   replicaCount, stripeCount,
                                                   transportList, force)

    @exportAsVerb
    def volumeStart(self, volumeName, force=False, options=None):
        self.svdsmProxy.glusterVolumeStart(volumeName, force)

    @exportAsVerb
    def volumeStop(self, volumeName, force=False, options=None):
        self.svdsmProxy.glusterVolumeStop(volumeName, force)

    @exportAsVerb
    def volumeDelete(self, volumeName, options=None):
        self.svdsmProxy.glusterVolumeDelete(volumeName)

    @exportAsVerb
    def volumeSet(self, volumeName, option, value, options=None):
        self.svdsmProxy.glusterVolumeSet(volumeName, option, value)

    @exportAsVerb
    def volumeSetOptionsList(self, options=None):
        return {'volumeSetOptions': self.svdsmProxy.glusterVolumeSetHelpXml()}

    @exportAsVerb
    def volumeReset(self, volumeName, option='', force=False, options=None):
        self.svdsmProxy.glusterVolumeReset(volumeName, option, force)

    @exportAsVerb
    def volumeBrickAdd(self, volumeName, brickList, replicaCount=0,
                       stripeCount=0, force=False, options=None):
        self.svdsmProxy.glusterVolumeAddBrick(volumeName,
                                              brickList,
                                              replicaCount,
                                              stripeCount,
                                              force)

    @exportAsVerb
    def volumeRebalanceStart(self, volumeName, rebalanceType="",
                             force=False, options=None):
        return self.svdsmProxy.glusterVolumeRebalanceStart(volumeName,
                                                           rebalanceType,
                                                           force)

    @exportAsVerb
    def volumeRebalanceStop(self, volumeName, force=False, options=None):
        return self.svdsmProxy.glusterVolumeRebalanceStop(volumeName, force)

    @exportAsVerb
    def volumeRebalanceStatus(self, volumeName, options=None):
        return self.svdsmProxy.glusterVolumeRebalanceStatus(volumeName)

    @exportAsVerb
    def volumeReplaceBrickStart(self, volumeName, existingBrick, newBrick,
                                options=None):
        return self.svdsmProxy.glusterVolumeReplaceBrickStart(volumeName,
                                                              existingBrick,
                                                              newBrick)

    @exportAsVerb
    def volumeReplaceBrickAbort(self, volumeName, existingBrick, newBrick,
                                options=None):
        self.svdsmProxy.glusterVolumeReplaceBrickAbort(volumeName,
                                                       existingBrick,
                                                       newBrick)

    @exportAsVerb
    def volumeReplaceBrickPause(self, volumeName, existingBrick, newBrick,
                                options=None):
        self.svdsmProxy.glusterVolumeReplaceBrickPause(volumeName,
                                                       existingBrick,
                                                       newBrick)

    @exportAsVerb
    def volumeReplaceBrickStatus(self, volumeName, oldBrick, newBrick,
                                 options=None):
        st, msg = self.svdsmProxy.glusterVolumeReplaceBrickStatus(volumeName,
                                                                  oldBrick,
                                                                  newBrick)
        return {'replaceBrick': st, 'message': msg}

    @exportAsVerb
    def volumeReplaceBrickCommit(self, volumeName, existingBrick, newBrick,
                                 force=False, options=None):
        self.svdsmProxy.glusterVolumeReplaceBrickCommit(volumeName,
                                                        existingBrick,
                                                        newBrick,
                                                        force)

    @exportAsVerb
    def volumeRemoveBrickStart(self, volumeName, brickList,
                               replicaCount=0, options=None):
        return self.svdsmProxy.glusterVolumeRemoveBrickStart(volumeName,
                                                             brickList,
                                                             replicaCount)

    @exportAsVerb
    def volumeRemoveBrickStop(self, volumeName, brickList,
                              replicaCount=0, options=None):
        return self.svdsmProxy.glusterVolumeRemoveBrickStop(volumeName,
                                                            brickList,
                                                            replicaCount)

    @exportAsVerb
    def volumeRemoveBrickStatus(self, volumeName, brickList,
                                replicaCount=0, options=None):
        return self.svdsmProxy.glusterVolumeRemoveBrickStatus(volumeName,
                                                              brickList,
                                                              replicaCount)

    @exportAsVerb
    def volumeRemoveBrickCommit(self, volumeName, brickList,
                                replicaCount=0, options=None):
        self.svdsmProxy.glusterVolumeRemoveBrickCommit(volumeName,
                                                       brickList,
                                                       replicaCount)

    @exportAsVerb
    def volumeRemoveBrickForce(self, volumeName, brickList,
                               replicaCount=0, options=None):
        self.svdsmProxy.glusterVolumeRemoveBrickForce(volumeName, brickList,
                                                      replicaCount)

    def _computeVolumeStats(self, data):
        total = data.f_blocks * data.f_bsize
        free = data.f_bfree * data.f_bsize
        used = total - free
        return {'sizeTotal': str(total),
                'sizeFree': str(free),
                'sizeUsed': str(used)}

    @exportAsVerb
    def volumeStatus(self, volumeName, brick=None, statusOption=None,
                     options=None):
        status = self.svdsmProxy.glusterVolumeStatus(volumeName, brick,
                                                     statusOption)
        if statusOption == 'detail':
            data = self.svdsmProxy.glusterVolumeStatvfs(volumeName)
            status['volumeStatsInfo'] = self._computeVolumeStats(data)
        return {'volumeStatus': status}

    @exportAsVerb
    def hostAdd(self, hostName, options=None):
        self.svdsmProxy.glusterPeerProbe(hostName)

    @exportAsVerb
    def hostRemove(self, hostName, force=False, options=None):
        self.svdsmProxy.glusterPeerDetach(hostName, force)

    @exportAsVerb
    def hostRemoveByUuid(self, hostUuid, force=False, options=None):
        for hostInfo in self.svdsmProxy.glusterPeerStatus():
            if hostInfo['uuid'] == hostUuid:
                hostName = hostInfo['hostname']
                break
        else:
            raise ge.GlusterHostNotFoundException()
        self.svdsmProxy.glusterPeerDetach(hostName, force)

    @exportAsVerb
    def hostsList(self, options=None):
        """
        Returns:
            {'status': {'code': CODE, 'message': MESSAGE},
             'hosts' : [{'hostname': HOSTNAME, 'uuid': UUID,
                         'status': STATE}, ...]}
        """
        return {'hosts': self.svdsmProxy.glusterPeerStatus()}

    @exportAsVerb
    def volumeProfileStart(self, volumeName, options=None):
        self.svdsmProxy.glusterVolumeProfileStart(volumeName)

    @exportAsVerb
    def volumeProfileStop(self, volumeName, options=None):
        self.svdsmProxy.glusterVolumeProfileStop(volumeName)

    @exportAsVerb
    def volumeProfileInfo(self, volumeName, nfs=False, options=None):
        status = self.svdsmProxy.glusterVolumeProfileInfo(volumeName, nfs)
        return {'profileInfo': status}

    @exportAsVerb
    def hooksList(self, options=None):
        status = self.svdsmProxy.glusterHooksList()
        return {'hooksList': status}

    @exportAsVerb
    def hookEnable(self, glusterCmd, hookLevel, hookName, options=None):
        self.svdsmProxy.glusterHookEnable(glusterCmd, hookLevel, hookName)

    @exportAsVerb
    def hookDisable(self, glusterCmd, hookLevel, hookName, options=None):
        self.svdsmProxy.glusterHookDisable(glusterCmd, hookLevel, hookName)

    @exportAsVerb
    def hookRead(self, glusterCmd, hookLevel, hookName, options=None):
        return self.svdsmProxy.glusterHookRead(glusterCmd, hookLevel,
                                               hookName)

    @exportAsVerb
    def hookUpdate(self, glusterCmd, hookLevel, hookName, hookData,
                   hookMd5Sum, options=None):
        self.svdsmProxy.glusterHookUpdate(glusterCmd, hookLevel, hookName,
                                          hookData, hookMd5Sum)

    @exportAsVerb
    def hookAdd(self, glusterCmd, hookLevel, hookName, hookData, hookMd5Sum,
                enable=False, options=None):
        self.svdsmProxy.glusterHookAdd(glusterCmd, hookLevel, hookName,
                                       hookData, hookMd5Sum, enable)

    @exportAsVerb
    def hookRemove(self, glusterCmd, hookLevel, hookName, options=None):
        self.svdsmProxy.glusterHookRemove(glusterCmd, hookLevel, hookName)

    @exportAsVerb
    def hostUUIDGet(self, options=None):
        return {'uuid': self.svdsmProxy.glusterHostUUIDGet()}

    @exportAsVerb
    def servicesAction(self, serviceNames, action, options=None):
        status = self.svdsmProxy.glusterServicesAction(serviceNames,
                                                       action)
        return {'services': status}

    @exportAsVerb
    def servicesGet(self, serviceNames, options=None):
        status = self.svdsmProxy.glusterServicesGet(serviceNames)
        return {'services': status}

    @exportAsVerb
    def tasksList(self, taskIds=[], options=None):
        status = self.svdsmProxy.glusterTasksList(taskIds)
        return {'tasks': status}

    @exportAsVerb
    def volumeStatsInfoGet(self, volumeName, options=None):
        data = self.svdsmProxy.glusterVolumeStatvfs(volumeName)
        return self._computeVolumeStats(data)

    @exportAsVerb
    def storageDevicesList(self, options=None):
        status = self.svdsmProxy.glusterStorageDevicesList()
        return {'deviceInfo': status}

    @exportAsVerb
    def volumeGeoRepSessionStart(self, volumeName, remoteHost,
                                 remoteVolumeName,
                                 force=False, options=None):
        self.svdsmProxy.glusterVolumeGeoRepSessionStart(volumeName,
                                                        remoteHost,
                                                        remoteVolumeName,
                                                        force)

    @exportAsVerb
    def volumeGeoRepSessionStop(self, volumeName, remoteHost, remoteVolumeName,
                                force=False, options=None):
        self.svdsmProxy.glusterVolumeGeoRepSessionStop(volumeName,
                                                       remoteHost,
                                                       remoteVolumeName,
                                                       force)

    @exportAsVerb
    def volumeGeoRepSessionList(self, volumeName=None, remoteHost=None,
                                remoteVolumeName=None, options=None):
        status = self.svdsmProxy.glusterVolumeGeoRepStatus(
            volumeName,
            remoteHost,
            remoteVolumeName
        )
        return {'sessions': status}

    @exportAsVerb
    def volumeGeoRepSessionStatus(self, volumeName, remoteHost,
                                  remoteVolumeName, options=None):
        status = self.svdsmProxy.glusterVolumeGeoRepStatus(
            volumeName,
            remoteHost,
            remoteVolumeName,
            detail=True
        )
        return {'sessionStatus': status}

    @exportAsVerb
    def volumeGeoRepSessionPause(self, volumeName, remoteHost,
                                 remoteVolumeName,
                                 force=False, options=None):
        self.svdsmProxy.glusterVolumeGeoRepSessionPause(volumeName,
                                                        remoteHost,
                                                        remoteVolumeName,
                                                        force)

    @exportAsVerb
    def volumeGeoRepSessionResume(self, volumeName, remoteHost,
                                  remoteVolumeName,
                                  force=False, options=None):
        self.svdsmProxy.glusterVolumeGeoRepSessionResume(volumeName,
                                                         remoteHost,
                                                         remoteVolumeName,
                                                         force)

    @exportAsVerb
    def volumeGeoRepConfigList(self, volumeName, remoteHost, remoteVolumeName,
                               options=None):
        status = self.svdsmProxy.glusterVolumeGeoRepConfig(
            volumeName,
            remoteHost,
            remoteVolumeName
        )
        return {'sessionConfig': status}

    @exportAsVerb
    def volumeGeoRepConfigSet(self, volumeName, remoteHost, remoteVolumeName,
                              optionName, optionValue, options=None):
        self.svdsmProxy.glusterVolumeGeoRepConfig(volumeName,
                                                  remoteHost,
                                                  remoteVolumeName,
                                                  optionName,
                                                  optionValue)

    @exportAsVerb
    def volumeGeoRepConfigReset(self, volumeName, remoteHost,
                                remoteVolumeName,
                                optionName, options=None):
        self.svdsmProxy.glusterVolumeGeoRepConfig(
            volumeName,
            remoteHost,
            remoteVolumeName,
            optionName)

    @exportAsVerb
    def volumeSnapshotCreate(self, volumeName, snapName,
                             snapDescription=None, force=False,
                             options=None):
        return self.svdsmProxy.glusterSnapshotCreate(
            volumeName,
            snapName,
            snapDescription,
            force
        )

    @exportAsVerb
    def volumeSnapshotDeleteAll(self, volumeName, options=None):
        self.svdsmProxy.glusterSnapshotDelete(volumeName=volumeName)

    @exportAsVerb
    def snapshotDelete(self, snapName, options=None):
        self.svdsmProxy.glusterSnapshotDelete(snapName=snapName)

    @exportAsVerb
    def snapshotActivate(self, snapName, force=False, options=None):
        self.svdsmProxy.glusterSnapshotActivate(snapName, force)

    @exportAsVerb
    def snapshotDeactivate(self, snapName, options=None):
        self.svdsmProxy.glusterSnapshotDeactivate(snapName)

    @exportAsVerb
    def snapshotRestore(self, snapName, options=None):
        status = self.svdsmProxy.glusterSnapshotRestore(snapName)
        return {'snapRestore': status}

    @exportAsVerb
    def snapshotConfigList(self, options=None):
        try:
            status = self.svdsmProxy.glusterSnapshotConfig()
        except ge.GlusterSnapshotConfigFailedException as e:
            raise ge.GlusterSnapshotConfigGetFailedException(rc=e.rc,
                                                             err=e.err)
        return {'snapshotConfig': status}

    @exportAsVerb
    def volumeSnapshotConfigList(self, volumeName, options=None):
        try:
            status = self.svdsmProxy.glusterSnapshotConfig(
                volumeName=volumeName)
        except ge.GlusterSnapshotConfigFailedException as e:
            raise ge.GlusterSnapshotConfigGetFailedException(rc=e.rc,
                                                             err=e.err)
        return {'snapshotConfig': status}

    @exportAsVerb
    def volumeSnapshotConfigSet(self, volumeName, optionName, optionValue,
                                options=None):
        try:
            self.svdsmProxy.glusterSnapshotConfig(volumeName=volumeName,
                                                  optionName=optionName,
                                                  optionValue=optionValue)
        except ge.GlusterSnapshotConfigFailedException as e:
            raise ge.GlusterSnapshotConfigSetFailedException(rc=e.rc,
                                                             err=e.err)

    @exportAsVerb
    def snapshotConfigSet(self, optionName, optionValue, options=None):
        try:
            self.svdsmProxy.glusterSnapshotConfig(optionName=optionName,
                                                  optionValue=optionValue)
        except ge.GlusterSnapshotConfigFailedException as e:
            raise ge.GlusterSnapshotConfigSetFailedException(rc=e.rc,
                                                             err=e.err)


def getGlusterMethods(gluster):
    l = []
    for name in dir(gluster):
        func = getattr(gluster, name)
        if getattr(func, 'exportAsVerb', False) is True:
            l.append((func, 'gluster%s%s' % (name[0].upper(), name[1:])))
    return tuple(l)
