#
# Copyright 2009-2012 Red Hat, Inc.
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

import os
import logging
import threading
import uuid
from contextlib import contextmanager

import volume
from sdc import sdCache
import sd
import misc
import fileUtils
from vdsm.config import config
import storage_exception as se
import task
from threadLocal import vars
import resourceFactories
import resourceManager as rm
import outOfProcess as oop
rmanager = rm.ResourceManager.getInstance()

# Disk type
UNKNOWN_DISK_TYPE = 0
SYSTEM_DISK_TYPE = 1
DATA_DISK_TYPE = 2
SHARED_DISK_TYPE = 3
SWAP_DISK_TYPE = 4
TEMP_DISK_TYPE = 5

DISK_TYPES = {UNKNOWN_DISK_TYPE:'UNKNOWN', SYSTEM_DISK_TYPE:'SYSTEM',
                DATA_DISK_TYPE:'DATA', SHARED_DISK_TYPE:'SHARED',
                SWAP_DISK_TYPE:'SWAP', TEMP_DISK_TYPE:'TEMP'}

# Image Operations
UNKNOWN_OP = 0
COPY_OP = 1
MOVE_OP = 2
OP_TYPES = {UNKNOWN_OP:'UNKNOWN', COPY_OP:'COPY', MOVE_OP:'MOVE'}

REMOVED_IMAGE_PREFIX = "_remove_me_"
RENAME_RANDOM_STRING_LEN = 8


class Image:
    """ Actually represents a whole virtual disk.
        Consist from chain of volumes.
    """
    log = logging.getLogger('Storage.Image')
    _fakeTemplateLock = threading.Lock()

    @classmethod
    def createImageRollback(cls, taskObj, imageDir):
        """
        Remove empty image folder
        """
        cls.log.info("createImageRollback: imageDir=%s" % (imageDir))
        if os.path.exists(imageDir):
            if not len(os.listdir(imageDir)):
                fileUtils.cleanupdir(imageDir)
            else:
                cls.log.error("createImageRollback: Cannot remove dirty image folder %s" % (imageDir))

    def __init__(self, repoPath):
        self.repoPath = repoPath
        self.storage_repository = config.get('irs', 'repository')

    def create(self, sdUUID, imgUUID):
        """Create placeholder for image's volumes
            'sdUUID' - storage domain UUID
            'imgUUID' - image UUID
        """
        imageDir = os.path.join(self.repoPath, sdUUID, sd.DOMAIN_IMAGES, imgUUID)
        if not os.path.isdir(imageDir):
            self.log.info("Create placeholder %s for image's volumes",
                imageDir)
            taskName = "create image rollback: " + imgUUID
            vars.task.pushRecovery(task.Recovery(taskName, "image", "Image", "createImageRollback",
                                                 [imageDir]))
            os.mkdir(imageDir)
        return imageDir

    def getImageDir(self, sdUUID, imgUUID):
        """
        Return image directory
        """
        return os.path.join(self.repoPath, sdUUID, sd.DOMAIN_IMAGES, imgUUID)

    def preDeleteHandler(self, sdUUID, imgUUID):
        """
        Pre-delete handler for images on backup domain
        """
        # We should handle 2 opposite scenarios:
        # 1. Remove template's image:  Create 'fake' template instead of deleted one
        # 2. Remove regular image:  Remove parent-'fake' template if nobody need it already
        try:
            pvol = self.getTemplate(sdUUID=sdUUID, imgUUID=imgUUID)
            # 1. If we required to delete template's image that have VMs
            # based on it, we should create similar 'fake' template instead
            if pvol:
                pvolParams = pvol.getVolumeParams()
                # Find out real imgUUID of parent volume
                pimg = pvolParams['imgUUID']
                # Check whether deleted image is a template itself
                if imgUUID == pimg:
                    imglist = pvol.findImagesByVolume()
                    if len(imglist) > 1:
                        return pvolParams

            # 2. If we required to delete regular (non-template) image, we should also
            # check its template (if exists) and in case that template is 'fake'
            # and no VMs based on it, remove it too.
            if pvol and pvol.isFake():
                # At this point 'pvol' is a fake template and we should find out all its children
                chList = pvol.getAllChildrenList(self.repoPath, sdUUID, pimg, pvol.volUUID)
                # If 'pvol' has more than one child don't touch it, else remove it
                if len(chList) <= 1:
                    # Delete 'fake' parent image before deletion required image
                    # will avoid situation which new image based on this 'fake' parent
                    # can be created.
                    self._delete(sdUUID=sdUUID, imgUUID=pimg, postZero=False, force=True)
        except se.StorageException:
            self.log.warning("Image %s in domain %s had problem during deletion process", imgUUID, sdUUID, exc_info=True)

        return None

    def delete(self, sdUUID, imgUUID, postZero, force):
        """
        Delete whole image
        """
        name = "delete image %s retry" % imgUUID
        vars.task.pushRecovery(task.Recovery(name, "image", "Image", "deleteRecover",
            [self.repoPath, sdUUID, imgUUID, str(postZero), str(force)]))

        try:
            self._delete(sdUUID, imgUUID, postZero, force)
        except se.StorageException:
            self.log.error("Unexpected error", exc_info=True)
            raise
        except Exception, e:
            self.log.error("Unexpected error", exc_info=True)
            raise se.ImageDeleteError("%s: %s" % (imgUUID, str(e)))

    @classmethod
    def deleteRecover(cls, taskObj, repoPath, sdUUID, imgUUID, postZero, force):
        """
        Delete image rollforward
        """
        Image(repoPath)._delete(sdUUID, imgUUID, misc.parseBool(postZero), misc.parseBool(force))

    def validateDelete(self, sdUUID, imgUUID):
        """
        Validate image before deleting
        """
        # Get the list of the volumes
        volclass = sdCache.produce(sdUUID).getVolumeClass()
        uuidlist = volclass.getImageVolumes(self.repoPath, sdUUID, imgUUID)
        volumes = [volclass(self.repoPath, sdUUID, imgUUID, volUUID) for volUUID in uuidlist]

        for vol in volumes:
            try:
                if vol.isShared():
                    images = vol.findImagesByVolume(legal=True)
                    if len(images) > 1:
                        msg = "Cannot delete image %s due to shared volume %s" % (imgUUID, vol.volUUID)
                        raise se.CannotDeleteSharedVolume(msg)
            except se.MetaDataKeyNotFoundError, e:
                # In case of metadata key error, we have corrupted
                # volume (One of metadata corruptions may be
                # previous volume deletion failure).
                # So, there is no reasons to avoid its deletion
                self.log.warn("Volume %s metadata error (%s)", vol.volUUID, str(e))
        return volumes

    def _delete(self, sdUUID, imgUUID, postZero, force):
        """Delete Image folder with all volumes
            'sdUUID' - storage domain UUID
            'imgUUID' - image UUID
            'force'   - make it brutal
        """
        # Get the list of the volumes
        volclass = sdCache.produce(sdUUID).getVolumeClass()
        uuidlist = volclass.getImageVolumes(self.repoPath, sdUUID, imgUUID)

        # If we are not 'force'd to remove check that there will be no issues
        if not force:
            volumes = self.validateDelete(sdUUID, imgUUID)
        else:
            volumes = [volclass(self.repoPath, sdUUID, imgUUID, volUUID) for volUUID in uuidlist]

        # If we got here than go ahead and remove all of them without mercy
        if volumes:
            try:
                mod = __import__(volclass.__module__,
                                 fromlist=['deleteMultipleVolumes'])
                mod.deleteMultipleVolumes(sdUUID, volumes, postZero)
            except (se.CannotRemoveLogicalVolume, se.VolumeAccessError):
                #Any volume deletion failed, but we don't really care at this point
                self.log.warn("Problems during image %s deletion. Continue...",
                              imgUUID, exc_info=True)

        # Now clean the image directory
        removedImage = imageDir = self.getImageDir(sdUUID, imgUUID)

        # If image directory doesn't exist we are done
        if not os.path.exists(imageDir):
            return True

        # Otherwise move it out of the way if it hasn't been moved yet
        if not imgUUID.startswith(REMOVED_IMAGE_PREFIX):
            removedImage = os.path.join(os.path.dirname(imageDir),
                REMOVED_IMAGE_PREFIX + os.path.basename(imageDir))
            os.rename(imageDir, removedImage)

        # Cleanup (hard|soft) links and other state files,
        # i.e. remove everything left including directory itself
        #
        # N.B. The cleanup can fail, but it doesn't bother us at all
        # since the image directory is removed and this image will not show up
        # in the image list anymore. If will be cleaned up at some later time
        # by one of the hosts running vdsm.
        #
        # Inquiring mind can notice that it might happen even on HSM (the
        # image removal itself only performed by SPM), but that is OK - we
        # are not touching any live data. We are removing garbage, that is not
        # used anyway.
        fileUtils.cleanupdir(removedImage)
        return True

    def deletedVolumeName(self, uuid):
        """
        Create REMOVED_IMAGE_PREFIX + <random> + uuid string.
        """
        randomStr = misc.randomStr(RENAME_RANDOM_STRING_LEN)
        return "%s%s_%s" % (REMOVED_IMAGE_PREFIX, randomStr, uuid)

    def preDeleteRename(self, sdUUID, imgUUID):
        # Get the list of the volumes
        volclass = sdCache.produce(sdUUID).getVolumeClass()
        uuidlist = volclass.getImageVolumes(self.repoPath, sdUUID, imgUUID)
        imageDir = self.getImageDir(sdUUID, imgUUID)

        # If image directory doesn't exist we are done
        if not os.path.exists(imageDir):
            return imgUUID

        # Otherwise move it out of the way
        newImgUUID = self.deletedVolumeName(imgUUID)
        self.log.info("Rename image %s -> %s", imgUUID, newImgUUID)
        if not imgUUID.startswith(REMOVED_IMAGE_PREFIX):
            removedImage = os.path.join(os.path.dirname(imageDir), newImgUUID)
            os.rename(imageDir, removedImage)
        else:
            self.log.warning("Image %s in domain %s already renamed", imgUUID, sdUUID)

        volumes = [volclass(self.repoPath, sdUUID, newImgUUID, volUUID) for volUUID in uuidlist]
        for vol in volumes:
            if not vol.volUUID.startswith(REMOVED_IMAGE_PREFIX):
                vol.rename(self.deletedVolumeName(vol.volUUID), recovery=False)
            else:
                self.log.warning("Volume %s of image %s already renamed", vol.volUUID, imgUUID)
            # We change image UUID in metadata
            # (and IU_ LV tag for block volumes) of all volumes in image
            vol.setImage(newImgUUID)

        return newImgUUID

    def __chainSizeCalc(self, sdUUID, imgUUID, volUUID, size):
        """
        Compute an estimate of the whole chain size
        using the sum of the actual size of the chain's volumes
        """
        chain = self.getChain(sdUUID, imgUUID, volUUID)
        newsize = 0
        template = chain[0].getParentVolume()
        if template:
            newsize = template.getVolumeSize()
        for vol in chain:
            newsize += vol.getVolumeSize()
        if newsize > size:
            newsize = size
        newsize = int(newsize * 1.1)    # allocate %10 more for cow metadata
        return newsize

    def getChain(self, sdUUID, imgUUID, volUUID=None):
        """
        Return the chain of volumes of image as a sorted list
        (not including a shared base (template) if any)
        """
        chain = []
        volclass = sdCache.produce(sdUUID).getVolumeClass()

        # Use volUUID when provided
        if volUUID:
            srcVol = volclass(self.repoPath, sdUUID, imgUUID, volUUID)

            # For template images include only one volume (the template itself)
            # NOTE: this relies on the fact that in a template there is only one
            #       volume
            if srcVol.isShared():
                return [srcVol]

        # Find all the volumes when volUUID is not provided
        else:
            # Find all volumes of image
            uuidlist = volclass.getImageVolumes(self.repoPath, sdUUID, imgUUID)

            if not uuidlist:
                raise se.ImageDoesNotExistInSD(imgUUID, sdUUID)

            srcVol = volclass(self.repoPath, sdUUID, imgUUID, uuidlist[0])

            # For template images include only one volume (the template itself)
            if len(uuidlist) == 1 and srcVol.isShared():
                return [srcVol]

            # Searching for the leaf
            for vol in uuidlist:
                srcVol = volclass(self.repoPath, sdUUID, imgUUID, vol)

                if srcVol.isLeaf():
                    break

                srcVol = None

            if not srcVol:
                self.log.error("There is no leaf in the image %s", imgUUID)
                raise se.ImageIsNotLegalChain(imgUUID)

        # Build up the sorted parent -> child chain
        while not srcVol.isShared():
            chain.insert(0, srcVol)

            if srcVol.getParent() == volume.BLANK_UUID:
                break

            srcVol = srcVol.getParentVolume()

        self.log.info("sdUUID=%s imgUUID=%s chain=%s ", sdUUID, imgUUID, chain)
        return chain

    def getTemplate(self, sdUUID, imgUUID):
        """
        Return template of the image
        """
        tmpl = None
        # Find all volumes of image (excluding template)
        chain = self.getChain(sdUUID, imgUUID)
        # check if the chain is build above a template, or it is a standalone
        pvol = chain[0].getParentVolume()
        if pvol:
            tmpl = pvol
        elif chain[0].isShared():
            tmpl = chain[0]

        return tmpl

    def prepare(self, sdUUID, imgUUID, volUUID=None):
        chain = self.getChain(sdUUID, imgUUID, volUUID)

        # Adding the image template to the chain
        tmplVolume = chain[0].getParentVolume()

        if tmplVolume:
            chain.insert(0, tmplVolume)

        # Activating the volumes
        sdCache.produce(sdUUID).activateVolumes(
                    volUUIDs=[vol.volUUID for vol in chain])

        return chain

    def teardown(self, sdUUID, imgUUID, volUUID=None):
        chain = self.getChain(sdUUID, imgUUID, volUUID)

        # Deactivating the volumes
        sdCache.produce(sdUUID).deactivateVolumes(
                    volUUIDs=[vol.volUUID for vol in chain])

        # Do not deactivate the template yet (might be in use by an other vm)
        # TODO: reference counting to deactivate when unused

    def __templateRelink(self, destDom, imgUUID, volUUID):
        """
        Relink all hardlinks of the template 'volUUID' in all VMs based on it.

        This function assumes that dom is backup dom and that template image is
        used by other volumes.
        """
        # Avoid relink templates for non-NFS domains
        if destDom.getStorageType() not in [ sd.NFS_DOMAIN ]:
            self.log.debug("Doesn't relink templates non-NFS domain %s", destDom.sdUUID)
            return

        allVols = destDom.getAllVolumes()
        tImgs = allVols[volUUID].imgs
        if len(tImgs) < 2:
            self.log.debug("Volume %s is an unused template or a regular "
                           "volume. Found  in images: %s allVols: %s", volUUID,
                           tImgs, allVols)
            return
        templateImage = tImgs[0]
        relinkImgs = tuple(tImgs[1:])
        for rImg in relinkImgs:
            # This function assumes that all relevant images and template
            # namespaces are locked.
            tLink = os.path.join(self.repoPath, destDom.sdUUID, sd.DOMAIN_IMAGES, rImg, volUUID)
            oop.getProcessPool(destDom.sdUUID).os.unlink(tLink)
            oop.getProcessPool(destDom.sdUUID).os.link(os.path.join(
                                self.repoPath, destDom.sdUUID, sd.DOMAIN_IMAGES,
                                templateImage, volUUID), tLink)

    def createFakeTemplate(self, sdUUID, volParams):
        """
        Create fake template (relevant for Backup domain only)
        """
        with self._fakeTemplateLock:
            try:
                destDom = sdCache.produce(sdUUID)
                volclass = destDom.getVolumeClass()
                # Validate that the destination template exists and accessible
                volclass(self.repoPath, sdUUID, volParams['imgUUID'], volParams['volUUID'])
            except (se.VolumeDoesNotExist, se.ImagePathError):
                try:
                    # Create fake parent volume
                    destDom.createVolume(imgUUID=volParams['imgUUID'], size=volParams['size'],
                                          volFormat=volume.COW_FORMAT, preallocate=volume.SPARSE_VOL,
                                          diskType=volParams['disktype'], volUUID=volParams['volUUID'], desc="Fake volume",
                                          srcImgUUID=volume.BLANK_UUID, srcVolUUID=volume.BLANK_UUID)

                    vol = destDom.produceVolume(imgUUID=volParams['imgUUID'], volUUID=volParams['volUUID'])
                    # Mark fake volume as "FAKE"
                    vol.setLegality(volume.FAKE_VOL)
                    # Mark fake volume as shared
                    vol.setShared()
                    # Now we should re-link all hardlinks of this template in all VMs based on it
                    self.__templateRelink(destDom, volParams['imgUUID'], volParams['volUUID'])

                    self.log.debug("Succeeded to create fake image %s in domain %s", volParams['imgUUID'], destDom.sdUUID)
                except Exception:
                    self.log.error("Failure to create fake image %s in domain %s", volParams['imgUUID'],
                        destDom.sdUUID, exc_info=True)

    def isLegal(self, sdUUID, imgUUID):
        """
        Check correctness of the whole chain (excluding template)
        """
        try:
            legal = True
            volclass = sdCache.produce(sdUUID).getVolumeClass()
            vollist = volclass.getImageVolumes(self.repoPath, sdUUID, imgUUID)
            self.log.info("image %s in domain %s has vollist %s", imgUUID, sdUUID, str(vollist))
            for v in vollist:
                vol = volclass(self.repoPath, sdUUID, imgUUID, v)
                if not vol.isLegal() or vol.isFake():
                    legal = False
                    break
        except:
            legal = False
        return legal

    def __cleanupMove(self, srcVol, dstVol):
        """
        Cleanup environments after move operation
        """
        try:
            if srcVol:
                srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID)
            if dstVol:
                dstVol.teardown(sdUUID=dstVol.sdUUID, volUUID=dstVol.volUUID)
        except Exception:
            self.log.error("Unexpected error", exc_info=True)

    def _createTargetImage(self, destDom, srcSdUUID, imgUUID):
        # Before actual data copying we need perform several operation
        # such as: create all volumes, create fake template if needed, ...
        try:
            # Find all volumes of source image
            srcChain = self.getChain(srcSdUUID, imgUUID)
        except se.StorageException:
            self.log.error("Unexpected error", exc_info=True)
            raise
        except Exception, e:
            self.log.error("Unexpected error", exc_info=True)
            raise se.SourceImageActionError(imgUUID, srcSdUUID, str(e))

        fakeTemplate = False
        pimg = volume.BLANK_UUID    # standalone chain
        # check if the chain is build above a template, or it is a standalone
        pvol = srcChain[0].getParentVolume()
        if pvol:
            # find out parent volume parameters
            volParams = pvol.getVolumeParams()
            pimg = volParams['imgUUID']      # pimg == template image
            if destDom.isBackup():
                # FIXME: This workaround help as copy VM to the backup domain without its template
                # We will create fake template for future VM creation and mark it as FAKE volume
                # This situation is relevant for backup domain only
                fakeTemplate = True

        @contextmanager
        def justLogIt(img):
            self.log.debug("You don't really need lock parent of image %s", img)
            yield

        dstImageResourcesNamespace = sd.getNamespace(destDom.sdUUID, resourceFactories.IMAGE_NAMESPACE)
        # In destination domain we need to lock image's template if exists
        with rmanager.acquireResource(dstImageResourcesNamespace, pimg, rm.LockType.shared) \
                        if pimg != volume.BLANK_UUID else justLogIt(imgUUID):
            if fakeTemplate:
                self.createFakeTemplate(destDom.sdUUID, volParams)

            dstChain = []
            for srcVol in srcChain:
                # Create the dst volume
                try:
                    # find out src volume parameters
                    volParams = srcVol.getVolumeParams(bs=1)

                    # To avoid 'prezeroing' preallocated volume on NFS domain,
                    # we create the target volume with minimal size and after that w'll change
                    # its metadata back to the original size.
                    tmpSize = 20480 # in sectors (10M)
                    destDom.createVolume(imgUUID=imgUUID, size=tmpSize,
                                         volFormat=volParams['volFormat'], preallocate=volParams['prealloc'],
                                         diskType=volParams['disktype'], volUUID=srcVol.volUUID, desc=volParams['descr'],
                                         srcImgUUID=pimg, srcVolUUID=volParams['parent'])
                    dstVol = destDom.produceVolume(imgUUID=imgUUID, volUUID=srcVol.volUUID)
                    # Extend volume (for LV only) size to the actual size
                    dstVol.extend((volParams['apparentsize'] + 511) / 512)
                    # Change destination volume metadata back to the original size.
                    dstVol.setSize(volParams['size'])
                    dstChain.append(dstVol)
                except se.StorageException:
                    self.log.error("Unexpected error", exc_info=True)
                    raise
                except Exception, e:
                    self.log.error("Unexpected error", exc_info=True)
                    raise se.DestImageActionError(imgUUID, destDom.sdUUID, str(e))

                # only base may have a different parent image
                pimg = imgUUID

        return {'srcChain':srcChain, 'dstChain':dstChain}

    def _interImagesCopy(self, destDom, srcSdUUID, imgUUID, chains):
        srcLeafVol = chains['srcChain'][-1]
        dstLeafVol = chains['dstChain'][-1]
        try:
            # Prepare the whole chains before the copy
            srcLeafVol.prepare(rw=False)
            dstLeafVol.prepare(rw=True, chainrw=True, setrw=True)
        except Exception:
            self.log.error("Unexpected error", exc_info=True)
            # teardown volumes
            self.__cleanupMove(srcLeafVol, dstLeafVol)
            raise

        try:
            for srcVol in chains['srcChain']:
                # Do the actual copy
                try:
                    dstVol = destDom.produceVolume(imgUUID=imgUUID, volUUID=srcVol.volUUID)
                    srcSize = srcVol.getVolumeSize(bs=1)
                    misc.ddWatchCopy(srcVol.getVolumePath(), dstVol.getVolumePath(), vars.task.aborting, size=srcSize)
                except se.ActionStopped:
                    raise
                except se.StorageException:
                    self.log.error("Unexpected error", exc_info=True)
                    raise
                except Exception:
                    self.log.error("Copy image error: image=%s, src domain=%s, dst domain=%s", imgUUID, srcSdUUID,
                                    destDom.sdUUID, exc_info=True)
                    raise se.CopyImageError()
        finally:
            # teardown volumes
            self.__cleanupMove(srcLeafVol, dstLeafVol)

    def _finalizeDestinationImage(self, destDom, imgUUID, chains, force):
        for srcVol in chains['srcChain']:
            try:
                dstVol = destDom.produceVolume(imgUUID=imgUUID, volUUID=srcVol.volUUID)
                # In case of copying template, we should set the destination volume
                #  as SHARED (after copy because otherwise prepare as RW would fail)
                if srcVol.isShared():
                    dstVol.setShared()
                elif srcVol.isInternal():
                    dstVol.setInternal()
            except se.StorageException:
                self.log.error("Unexpected error", exc_info=True)
                raise
            except Exception, e:
                self.log.error("Unexpected error", exc_info=True)
                raise se.DestImageActionError(imgUUID, destDom.sdUUID, str(e))

    def move(self, srcSdUUID, dstSdUUID, imgUUID, vmUUID, op, postZero, force):
        """
        Move/Copy image between storage domains within same storage pool
        """
        self.log.info("srcSdUUID=%s dstSdUUID=%s "\
            "imgUUID=%s vmUUID=%s op=%s force=%s postZero=%s",
            srcSdUUID, dstSdUUID, imgUUID, vmUUID, OP_TYPES[op], str(force), str(postZero))

        destDom = sdCache.produce(dstSdUUID)
        # If image already exists check whether it illegal/fake, overwrite it
        if not self.isLegal(destDom.sdUUID, imgUUID):
            force = True
        # We must first remove the previous instance of image (if exists)
        # in destination domain, if we got the overwrite command
        if force:
            self.log.info("delete image %s on domain %s before overwriting", imgUUID, destDom.sdUUID)
            self.delete(destDom.sdUUID, imgUUID, postZero, force=True)

        chains = self._createTargetImage(destDom, srcSdUUID, imgUUID)
        self._interImagesCopy(destDom, srcSdUUID, imgUUID, chains)
        self._finalizeDestinationImage(destDom, imgUUID, chains, force)
        if force:
            leafVol = chains['dstChain'][-1]
            # Now we should re-link all deleted hardlinks, if exists
            self.__templateRelink(destDom, imgUUID, leafVol.volUUID)

        # At this point we successfully finished the 'copy' part of the operation
        # and we can clear all recoveries.
        vars.task.clearRecoveries()
        # If it's 'move' operation, we should delete src image after copying
        if op == MOVE_OP:
            self.delete(srcSdUUID, imgUUID, postZero, force=True)

        self.log.info("%s task on image %s was successfully finished", OP_TYPES[op], imgUUID)
        return True

    def __cleanupMultimove(self, sdUUID, imgList, postZero=False):
        """
        Cleanup environments after multiple-move operation
        """
        for imgUUID in imgList:
            try:
                self.delete(sdUUID, imgUUID, postZero, force=True)
            except Exception:
                pass

    def multiMove(self, srcSdUUID, dstSdUUID, imgDict, vmUUID, force):
        """
        Move multiple images between storage domains within same storage pool
        """
        self.log.info("srcSdUUID=%s dstSdUUID=%s imgDict=%s vmUUID=%s force=%s",
            srcSdUUID, dstSdUUID, str(imgDict), vmUUID, str(force))

        cleanup_candidates = []
        # First, copy all images to the destination domain
        for (imgUUID, postZero) in imgDict.iteritems():
            self.log.info("srcSdUUID=%s dstSdUUID=%s imgUUID=%s postZero=%s",
                srcSdUUID, dstSdUUID, imgUUID, postZero)
            try:
                self.move(srcSdUUID, dstSdUUID, imgUUID, vmUUID, COPY_OP, postZero, force)
            except se.StorageException:
                self.__cleanupMultimove(sdUUID=dstSdUUID, imgList=cleanup_candidates, postZero=postZero)
                raise
            except Exception, e:
                self.__cleanupMultimove(sdUUID=dstSdUUID, imgList=cleanup_candidates, postZero=postZero)
                self.log.error(e, exc_info=True)
                raise se.CopyImageError("image=%s, src domain=%s, dst domain=%s: msg %s" % (imgUUID, srcSdUUID, dstSdUUID, str(e)))

            cleanup_candidates.append(imgUUID)
        # Remove images from source domain only after successfull copying of all images to the destination domain
        for (imgUUID, postZero) in imgDict.iteritems():
            try:
                self.delete(srcSdUUID, imgUUID, postZero, force=True)
            except Exception:
                pass

    def __cleanupCopy(self, srcVol, dstVol):
        """
        Cleanup environments after copy operation
        """
        try:
            if srcVol:
                srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID)
            if dstVol:
                dstVol.teardown(sdUUID=dstVol.sdUUID, volUUID=dstVol.volUUID)
        except Exception:
            self.log.error("Unexpected error", exc_info=True)

    def validateVolumeChain(self, sdUUID, imgUUID):
        """
        Check correctness of the whole chain (including template if exists)
        """
        if not self.isLegal(sdUUID, imgUUID):
            raise se.ImageIsNotLegalChain(imgUUID)
        chain = self.getChain(sdUUID, imgUUID)
        # check if the chain is build above a template, or it is a standalone
        pvol = chain[0].getParentVolume()
        if pvol:
            if not pvol.isLegal() or pvol.isFake():
                raise se.ImageIsNotLegalChain(imgUUID)

    def copy(self, sdUUID, vmUUID, srcImgUUID, srcVolUUID, dstImgUUID, dstVolUUID,
             descr, dstSdUUID, volType, volFormat, preallocate, postZero, force):
        """
        Create new template/volume from VM.
        Do it by collapse and copy the whole chain (baseVolUUID->srcVolUUID)
        """
        self.log.info("sdUUID=%s vmUUID=%s "\
            "srcImgUUID=%s srcVolUUID=%s dstImgUUID=%s dstVolUUID=%s dstSdUUID=%s volType=%s"\
            " volFormat=%s preallocate=%s force=%s postZero=%s", sdUUID, vmUUID, srcImgUUID, srcVolUUID,
            dstImgUUID, dstVolUUID, dstSdUUID, volType, volume.type2name(volFormat),
            volume.type2name(preallocate), str(force), str(postZero))
        try:
            srcVol = dstVol = None

            # Find out dest sdUUID
            if dstSdUUID == sd.BLANK_UUID:
                dstSdUUID = sdUUID
            volclass = sdCache.produce(sdUUID).getVolumeClass()
            destDom = sdCache.produce(dstSdUUID)

            # find src volume
            try:
                srcVol = volclass(self.repoPath, sdUUID, srcImgUUID, srcVolUUID)
            except se.StorageException:
                raise
            except Exception, e:
                self.log.error(e, exc_info=True)
                raise se.SourceImageActionError(srcImgUUID, sdUUID, str(e))

            # Create dst volume
            try:
                # find out src volume parameters
                volParams = srcVol.getVolumeParams()

                if volParams['parent'] and volParams['parent'] != volume.BLANK_UUID:
                    # Volume has parent and therefore is a part of a chain
                    # in that case we can not know what is the exact size of
                    # the space target file (chain ==> cow ==> sparse).
                    # Therefore compute an estimate of the target volume size
                    # using the sum of the actual size of the chain's volumes
                    if volParams['volFormat'] != volume.COW_FORMAT or volParams['prealloc'] != volume.SPARSE_VOL:
                        raise se.IncorrectFormat(self)
                    volParams['apparentsize'] = self.__chainSizeCalc(sdUUID, srcImgUUID,
                                                                   srcVolUUID, volParams['size'])

                # Find out dest volume parameters
                if preallocate in [volume.PREALLOCATED_VOL, volume.SPARSE_VOL]:
                    volParams['prealloc'] = preallocate
                if volFormat in [volume.COW_FORMAT, volume.RAW_FORMAT]:
                    dstVolFormat = volFormat
                else:
                    dstVolFormat = volParams['volFormat']

                self.log.info("copy source %s:%s:%s vol size %s destination %s:%s:%s apparentsize %s" % (
                              sdUUID, srcImgUUID, srcVolUUID, volParams['size'], dstSdUUID, dstImgUUID,
                              dstVolUUID, volParams['apparentsize']))

                # If image already exists check whether it illegal/fake, overwrite it
                if not self.isLegal(dstSdUUID, dstImgUUID):
                    force = True

                # We must first remove the previous instance of image (if exists)
                # in destination domain, if we got the overwrite command
                if force:
                    self.log.info("delete image %s on domain %s before overwriting", dstImgUUID, dstSdUUID)
                    self.delete(dstSdUUID, dstImgUUID, postZero, force=True)

                # To avoid 'prezeroing' preallocated volume on NFS domain,
                # we create the target volume with minimal size and after that w'll change
                # its metadata back to the original size.
                tmpSize = 20480 # in sectors (10M)
                destDom.createVolume(imgUUID=dstImgUUID, size=tmpSize,
                                      volFormat=dstVolFormat, preallocate=volParams['prealloc'],
                                      diskType=volParams['disktype'], volUUID=dstVolUUID, desc=descr,
                                      srcImgUUID=volume.BLANK_UUID, srcVolUUID=volume.BLANK_UUID)

                dstVol = sdCache.produce(dstSdUUID).produceVolume(imgUUID=dstImgUUID, volUUID=dstVolUUID)
                # For convert to 'raw' we need use the virtual disk size instead of apparent size
                if dstVolFormat == volume.RAW_FORMAT:
                    newsize = volParams['size']
                else:
                    newsize = volParams['apparentsize']
                dstVol.extend(newsize)
                dstPath = dstVol.getVolumePath()
                # Change destination volume metadata back to the original size.
                dstVol.setSize(volParams['size'])
            except se.StorageException, e:
                self.log.error("Unexpected error", exc_info=True)
                raise
            except Exception, e:
                self.log.error("Unexpected error", exc_info=True)
                raise se.CopyImageError("Destination volume %s error: %s" % (dstVolUUID, str(e)))

            try:
                # Start the actual copy image procedure
                srcVol.prepare(rw=False)
                dstVol.prepare(rw=True, setrw=True)

                try:
                    (rc, out, err) = volume.qemuConvert(volParams['path'], dstPath,
                        volParams['volFormat'], dstVolFormat, vars.task.aborting,
                        size=srcVol.getVolumeSize(bs=1), dstvolType=dstVol.getType())
                    if rc:
                        raise se.StorageException("rc: %s, err: %s" % (rc, err))
                except se.ActionStopped, e:
                    raise e
                except se.StorageException, e:
                    raise se.CopyImageError(str(e))

                # Mark volume as SHARED
                if volType == volume.SHARED_VOL:
                    dstVol.setShared()

                if force:
                    # Now we should re-link all deleted hardlinks, if exists
                    self.__templateRelink(destDom, dstImgUUID, dstVolUUID)
            except se.StorageException, e:
                self.log.error("Unexpected error", exc_info=True)
                raise
            except Exception, e:
                self.log.error("Unexpected error", exc_info=True)
                raise se.CopyImageError("src image=%s, dst image=%s: msg=%s" % (srcImgUUID, dstImgUUID, str(e)))

            self.log.info("Finished copying %s:%s -> %s:%s", sdUUID, srcVolUUID, dstSdUUID, dstVolUUID)
            #TODO: handle return status
            return dstVolUUID
        finally:
            self.__cleanupCopy(srcVol=srcVol, dstVol=dstVol)

    def markIllegalSubChain(self, sdDom, imgUUID, chain):
        """
        Mark all volumes in the sub-chain as illegal
        """
        if not chain:
            raise se.InvalidParameterException("chain", str(chain))

        volclass = sdDom.getVolumeClass()
        ancestor = chain[0]
        successor = chain[-1]
        tmpVol = volclass(self.repoPath, sdDom.sdUUID, imgUUID, successor)
        dstParent = volclass(self.repoPath, sdDom.sdUUID, imgUUID, ancestor).getParent()

        # Mark all volumes as illegal
        while tmpVol and dstParent != tmpVol.volUUID:
            vol = tmpVol.getParentVolume()
            tmpVol.setLegality(volume.ILLEGAL_VOL)
            tmpVol = vol

    def __teardownSubChain(self, sdUUID, imgUUID, chain):
        """
        Teardown all volumes in the sub-chain
        """
        if not chain:
            raise se.InvalidParameterException("chain", str(chain))

        # Teardown subchain ('ancestor' ->...-> 'successor') volumes
        # before they will deleted.
        # This subchain include volumes that were merged (rebased)
        # into 'successor' and now should be deleted.
        # We prepared all these volumes as part of preparing the whole
        # chain before rebase, but during rebase we detached all of them from the chain
        # and couldn't teardown they properly.
        # So, now we must teardown them to release they resources.
        volclass = sdCache.produce(sdUUID).getVolumeClass()
        ancestor = chain[0]
        successor = chain[-1]
        srcVol = volclass(self.repoPath, sdUUID, imgUUID, successor)
        dstParent = volclass(self.repoPath, sdUUID, imgUUID, ancestor).getParent()

        while srcVol and dstParent != srcVol.volUUID:
            try:
                self.log.info("Teardown volume %s from image %s", srcVol.volUUID, imgUUID)
                vol = srcVol.getParentVolume()
                srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID, justme=True)
                srcVol = vol
            except Exception:
                self.log.info("Failure to teardown volume %s in subchain %s -> %s", srcVol.volUUID,
                              ancestor, successor, exc_info=True)

    def removeSubChain(self, sdDom, imgUUID, chain, postZero):
        """
        Remove all volumes in the sub-chain
        """
        if not chain:
            raise se.InvalidParameterException("chain", str(chain))

        volclass = sdDom.getVolumeClass()
        ancestor = chain[0]
        successor = chain[-1]
        srcVol = volclass(self.repoPath, sdDom.sdUUID, imgUUID, successor)
        dstParent = volclass(self.repoPath, sdDom.sdUUID, imgUUID, ancestor).getParent()

        while srcVol and dstParent != srcVol.volUUID:
            self.log.info("Remove volume %s from image %s", srcVol.volUUID, imgUUID)
            vol = srcVol.getParentVolume()
            srcVol.delete(postZero=postZero, force=True)
            chain.remove(srcVol.volUUID)
            srcVol = vol

    def _internalVolumeMerge(self, sdDom, srcVolParams, volParams, newSize, chain):
        """
        Merge internal volume
        """
        srcVol = sdDom.produceVolume(imgUUID=srcVolParams['imgUUID'], volUUID=srcVolParams['volUUID'])
        # Extend successor volume to new accumulated subchain size
        srcVol.extend(newSize)

        srcVol.prepare(rw=True, chainrw=True, setrw=True)
        try:
            backingVolPath = os.path.join('..', srcVolParams['imgUUID'], volParams['volUUID'])
            srcVol.rebase(volParams['volUUID'], backingVolPath, volParams['volFormat'], unsafe=False, rollback=True)
        finally:
            srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID)

        # Prepare chain for future erase
        chain.remove(srcVolParams['volUUID'])
        self.__teardownSubChain(sdDom.sdUUID, srcVolParams['imgUUID'], chain)

        return chain

    def _baseCowVolumeMerge(self, sdDom, srcVolParams, volParams, newSize, chain):
        """
        Merge snapshot with base COW volume
        """
        # FIXME!!! In this case we need workaround to rebase successor
        # and transform it to be a base volume (without pointing to any backing volume).
        # Actually this case should be handled by 'qemu-img rebase' (RFE to kvm).
        # At this point we can achieve this result by 4 steps procedure:
        # Step 1: create temporary empty volume similar to ancestor volume
        # Step 2: Rebase (safely) successor volume on top of this temporary volume
        # Step 3: Rebase (unsafely) successor volume on top of "" (empty string)
        # Step 4: Delete temporary volume
        srcVol = sdDom.produceVolume(imgUUID=srcVolParams['imgUUID'], volUUID=srcVolParams['volUUID'])
        # Extend successor volume to new accumulated subchain size
        srcVol.extend(newSize)
        # Step 1: Create temporary volume with destination volume's parent parameters
        newUUID = str(uuid.uuid4())
        sdDom.createVolume(imgUUID=srcVolParams['imgUUID'],
                                         size=volParams['size'], volFormat=volParams['volFormat'],
                                         preallocate=volume.SPARSE_VOL, diskType=volParams['disktype'],
                                         volUUID=newUUID, desc="New base volume",
                                         srcImgUUID=volume.BLANK_UUID, srcVolUUID=volume.BLANK_UUID)

        tmpVol = sdDom.produceVolume(imgUUID=srcVolParams['imgUUID'], volUUID=newUUID)
        tmpVol.prepare(rw=True, justme=True, setrw=True)

        # We should prepare/teardown volume for every single rebase.
        # The reason is recheckIfLeaf at the end of the rebase, that change
        # volume permissions to RO for internal volumes.
        srcVol.prepare(rw=True, chainrw=True, setrw=True)
        try:
            # Step 2: Rebase successor on top of tmpVol
            #   qemu-img rebase -b tmpBackingFile -F backingFormat -f srcFormat src
            backingVolPath = os.path.join('..', srcVolParams['imgUUID'], newUUID)
            srcVol.rebase(newUUID, backingVolPath, volParams['volFormat'], unsafe=False, rollback=True)
        finally:
            srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID)

        srcVol.prepare(rw=True, chainrw=True, setrw=True)
        try:
            # Step 3: Remove pointer to backing file from the successor by 'unsafed' rebase
            #   qemu-img rebase -u -b "" -F backingFormat -f srcFormat src
            srcVol.rebase(volume.BLANK_UUID, "", volParams['volFormat'], unsafe=True, rollback=False)
        finally:
            srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID)

        # Step 4: Delete temporary volume
        tmpVol.teardown(sdUUID=tmpVol.sdUUID, volUUID=tmpVol.volUUID, justme=True)
        tmpVol.delete(postZero=False, force=True)

        # Prepare chain for future erase
        chain.remove(srcVolParams['volUUID'])
        self.__teardownSubChain(sdDom.sdUUID, srcVolParams['imgUUID'], chain)

        return chain

    def _baseRawVolumeMerge(self, sdDom, srcVolParams, volParams, chain):
        """
        Merge snapshot with base RAW volume
        """
        # In this case we need convert ancestor->successor subchain to new volume
        # and rebase successor's children (if exists) on top of it.
        # Step 1: Create an empty volume named sucessor_MERGE similar to ancestor volume.
        # Step 2: qemuConvert successor -> sucessor_MERGE
        # Step 3: Rename successor to _remove_me__successor
        # Step 4: Rename successor_MERGE to successor
        # Step 5: Unsafely rebase successor's children on top of temporary volume
        srcVol = chain[-1]
        srcVol.prepare(rw=True, chainrw=True, setrw=True)
        # Find out successor's children list
        chList = srcVolParams['childrenUUIDs']
        # Step 1: Create an empty volume named sucessor_MERGE with destination volume's parent parameters
        newUUID = srcVol.volUUID + "_MERGE"
        sdDom.createVolume(imgUUID=srcVolParams['imgUUID'],
                                         size=volParams['size'], volFormat=volParams['volFormat'],
                                         preallocate=volParams['prealloc'], diskType=volParams['disktype'],
                                         volUUID=newUUID, desc=srcVolParams['descr'],
                                         srcImgUUID=volume.BLANK_UUID, srcVolUUID=volume.BLANK_UUID)

        newVol = sdDom.produceVolume(imgUUID=srcVolParams['imgUUID'], volUUID=newUUID)
        newVol.prepare(rw=True, justme=True, setrw=True)

        # Step 2: Convert successor to new volume
        #   qemu-img convert -f qcow2 successor -O raw newUUID
        (rc, out, err) = volume.qemuConvert(srcVolParams['path'], newVol.getVolumePath(),
            srcVolParams['volFormat'], volParams['volFormat'], vars.task.aborting,
            size=volParams['apparentsize'], dstvolType=newVol.getType())
        if rc:
            raise se.MergeSnapshotsError(newUUID)

        newVol.teardown(sdUUID=newVol.sdUUID, volUUID=newVol.volUUID)
        srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID)
        if chList:
            newVol.setInternal()

        # Step 3: Rename successor as to _remove_me__successor
        tmpUUID = self.deletedVolumeName(srcVol.volUUID)
        # Step 4: Rename successor_MERGE to successor
        srcVol.rename(tmpUUID)
        newVol.rename(srcVolParams['volUUID'])

        # Step 5: Rebase children 'unsafely' on top of new volume
        #   qemu-img rebase -u -b tmpBackingFile -F backingFormat -f srcFormat src
        for v in chList:
            ch = sdDom.produceVolume(imgUUID=srcVolParams['imgUUID'], volUUID=v['volUUID'])
            ch.prepare(rw=True, chainrw=True, setrw=True, force=True)
            try:
                backingVolPath = os.path.join('..', srcVolParams['imgUUID'], srcVolParams['volUUID'])
                ch.rebase(srcVolParams['volUUID'], backingVolPath, volParams['volFormat'], unsafe=True, rollback=True)
            finally:
                ch.teardown(sdUUID=ch.sdUUID, volUUID=ch.volUUID)
            ch.recheckIfLeaf()

        # Prepare chain for future erase
        rmChain = [vol.volUUID for vol in chain if srcVol.volUUID != srcVolParams['volUUID']]
        rmChain.append(tmpUUID)

        return rmChain

    def subChainSizeCalc(self, ancestor, successor, vols):
        """
        Do not add additional calls to this function.

        TODO:
        Should be unified with chainSizeCalc,
        but merge should be refactored,
        but this file should probably removed.
        """
        chain = []
        accumulatedChainSize = 0
        endVolName = vols[ancestor].getParent() # TemplateVolName or None
        currVolName = successor
        while (currVolName != endVolName):
            chain.insert(0, currVolName)
            accumulatedChainSize += vols[currVolName].getVolumeSize()
            currVolName = vols[currVolName].getParent()

        return accumulatedChainSize, chain

    def merge(self, sdUUID, vmUUID, imgUUID, ancestor, successor, postZero):
        """Merge source volume to the destination volume.
            'successor' - source volume UUID
            'ancestor' - destination volume UUID
        """
        self.log.info("sdUUID=%s vmUUID=%s"\
                      " imgUUID=%s ancestor=%s successor=%s postZero=%s",
                      sdUUID, vmUUID, imgUUID,
                      ancestor, successor, str(postZero))
        sdDom = sdCache.produce(sdUUID)
        allVols = sdDom.getAllVolumes()
        volsImgs = sd.getVolsOfImage(allVols, imgUUID)
        # Since image namespace should be locked is produce all the volumes is
        # safe. Producing the (eventual) template is safe also.
        # TODO: Split for block and file based volumes for efficiency sake.
        vols = {}
        for vName in volsImgs.iterkeys():
            vols[vName] = sdDom.produceVolume(imgUUID, vName)

        srcVol = vols[successor]
        srcVolParams = srcVol.getVolumeParams()
        srcVolParams['childrenUUIDs'] = []
        for vName, vol in vols.iteritems():
            if vol.getParent() == successor:
                srcVolParams['childrenUUIDs'].append(vol.volUUID)
        dstVol = vols[ancestor]
        dstParentUUID = dstVol.getParent()
        if dstParentUUID != sd.BLANK_UUID:
            volParams = vols[dstParentUUID].getVolumeParams()
        else:
            volParams = dstVol.getVolumeParams()

        accSize, chain = self.subChainSizeCalc(ancestor, successor, vols)
        imageApparentSize = volParams['size']
        # allocate %10 more for cow metadata
        reqSize = min(accSize, imageApparentSize) * 1.1
        try:
            # Start the actual merge image procedure
            if dstParentUUID != sd.BLANK_UUID:
                # The ancestor isn't a base volume of the chain.
                self.log.info("Internal volume merge: src = %s dst = %s", srcVol.getVolumePath(), dstVol.getVolumePath())
                chainToRemove = self._internalVolumeMerge(sdDom, srcVolParams, volParams, reqSize, chain)
            # The ancestor is actually a base volume of the chain.
            # We have 2 cases here:
            # Case 1: ancestor is a COW volume (use 'rebase' workaround)
            # Case 2: ancestor is a RAW volume (use 'convert + rebase')
            elif volParams['volFormat'] == volume.RAW_FORMAT:
                self.log.info("merge with convert: src = %s dst = %s", srcVol.getVolumePath(), dstVol.getVolumePath())
                chainToRemove = self._baseRawVolumeMerge(sdDom, srcVolParams, volParams, [vols[vName] for vName in chain])
            else:
                self.log.info("4 steps merge: src = %s dst = %s", srcVol.getVolumePath(), dstVol.getVolumePath())
                chainToRemove = self._baseCowVolumeMerge(sdDom, srcVolParams, volParams, reqSize, chain)

            # This is unrecoverable point, clear all recoveries
            vars.task.clearRecoveries()
            # mark all snapshots from 'ancestor' to 'successor' as illegal
            self.markIllegalSubChain(sdDom, imgUUID, chainToRemove)
        except se.ActionStopped:
            raise
        except se.StorageException:
            self.log.error("Unexpected error", exc_info=True)
            raise
        except Exception, e:
            self.log.error(e, exc_info=True)
            raise se.SourceImageActionError(imgUUID, sdUUID, str(e))

        try:
            # remove all snapshots from 'ancestor' to 'successor'
            self.removeSubChain(sdDom, imgUUID, chainToRemove, postZero)
        except Exception:
            self.log.error("Failure to remove subchain %s -> %s in image %s", ancestor,
                           successor, imgUUID, exc_info=True)

        self.log.info("Merge src=%s with dst=%s was successfully finished.", srcVol.getVolumePath(), dstVol.getVolumePath())

