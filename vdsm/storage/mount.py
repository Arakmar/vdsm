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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from collections import namedtuple
import errno
from os.path import normpath
import re
import os
import stat
import threading

from vdsm import constants
import misc

# Common vfs types

VFS_NFS = "nfs"
VFS_NFS4 = "nfs4"
VFS_EXT3 = "ext3"

MountRecord = namedtuple("MountRecord", "fs_spec fs_file fs_vfstype "
                         "fs_mntops fs_freq fs_passno")

_ETC_MTAB_PATH = '/etc/mtab'
_PROC_MOUNTS_PATH = '/proc/mounts'
_SYS_DEV_BLOCK_PATH = '/sys/dev/block/'

_RE_ESCAPE = re.compile(r"\\0\d\d")


def _parseFstabLine(line):
    (fs_spec, fs_file, fs_vfstype, fs_mntops,
     fs_freq, fs_passno) = line.split()[:6]
    fs_mntops = fs_mntops.split(",")
    fs_freq = int(fs_freq)
    fs_passno = int(fs_passno)
    fs_spec = normpath(_parseFstabPath(fs_spec))

    fs_file = normpath(_parseFstabPath(fs_file))
    for suffix in (" (deleted)", ):
        if not fs_file.endswith(suffix):
            continue

        fs_file = fs_file[:-len(suffix)]
        break

    fs_mntops = [_parseFstabPath(item) for item in fs_mntops]

    return MountRecord(fs_spec, fs_file, fs_vfstype, fs_mntops,
                       fs_freq, fs_passno)


def _iterateMtab():
    with open(_ETC_MTAB_PATH, "r") as f:
        for line in f:
            yield _parseFstabLine(line)


def _parseFstabPath(path):
    return _RE_ESCAPE.sub(lambda s: chr(int(s.group()[1:], 8)), path)


class MountError(RuntimeError):
    pass


_loopFsSpecsLock = threading.Lock()
_loopFsSpecs = {}
_loopFsSpecsTimestamp = None


def _getLoopFsSpecs():
    with _loopFsSpecsLock:
        mtabTimestamp = os.stat(_ETC_MTAB_PATH).st_mtime
        if _loopFsSpecsTimestamp != mtabTimestamp:
            global _loopFsSpecs
            _loopFsSpecs = {}
            for entry in _iterateMtab():
                for opt in entry.fs_mntops:
                    if opt.startswith('loop='):
                        _loopFsSpecs[opt[len('loop='):]] = entry.fs_spec
            global _loopFsSpecsTimestamp
            _loopFsSpecsTimestamp = mtabTimestamp
    return _loopFsSpecs


def _resolveLoopDevice(path):
    """
    Loop devices appear as the loop device under /proc/mount instead of the
    backing file. As the mount command does the resolution so must we.
    """
    if not path.startswith("/"):
        return path

    try:
        st = os.stat(path)
    except:
        return path

    if not stat.S_ISBLK(st.st_mode):
        return path

    minor = os.minor(st.st_rdev)
    major = os.major(st.st_rdev)
    loopdir = os.path.join(_SYS_DEV_BLOCK_PATH,
                           '%d:%d' % (major, minor),
                           'loop')
    if os.path.exists(loopdir):
        with open(loopdir + "/backing_file", "r") as f:
            # Remove trailing newline
            return f.read()[:-1]

    # Old kernels might not have the sysfs entry, this is a bit slower and does
    # not work on hosts that do support the above method.

    lookup = _getLoopFsSpecs()

    if path in lookup:
        return lookup[path]

    return path


def _iterKnownMounts():
    with open(_PROC_MOUNTS_PATH, "r") as f:
        for line in f:
            yield _parseFstabLine(line)


def _iterMountRecords():
    for rec in _iterKnownMounts():
        realSpec = _resolveLoopDevice(rec.fs_spec)
        if rec.fs_spec == realSpec:
            yield rec
            continue

        yield MountRecord(realSpec, rec.fs_file, rec.fs_vfstype,
                          rec.fs_mntops, rec.fs_freq, rec.fs_passno)


def iterMounts():
    for record in _iterMountRecords():
        yield Mount(record.fs_spec, record.fs_file)


def isMounted(target):
    """Checks if a target is mounted at least once"""
    try:
        getMountFromTarget(target)
        return True
    except OSError as ex:
        if ex.errno == errno.ENOENT:
            return False
        raise


def getMountFromTarget(target):
    target = normpath(target)
    for rec in _iterMountRecords():
        if rec.fs_file == target:
            return Mount(rec.fs_spec, rec.fs_file)

    raise OSError(errno.ENOENT, 'Mount target %s not found' % target)


def getMountFromDevice(device):
    device = normpath(device)
    for rec in _iterMountRecords():
        if rec.fs_spec == device:
            return Mount(rec.fs_spec, rec.fs_file)

    raise OSError(errno.ENOENT, 'device %s not mounted' % device)


class Mount(object):
    def __init__(self, fs_spec, fs_file):
        self.fs_spec = normpath(fs_spec)
        self.fs_file = normpath(fs_file)

    def __eq__(self, other):
        if not isinstance(other, Mount):
            return False

        try:
            return (other.fs_spec == self.fs_spec and
                    other.fs_file == self.fs_file)
        except Exception:
            return False

    def __hash__(self):
        hsh = hash(type(self))
        hsh ^= hash(self.fs_spec)
        hsh ^= hash(self.fs_file)
        return hsh

    def mount(self, mntOpts=None, vfstype=None, timeout=None):
        cmd = [constants.EXT_MOUNT]

        if vfstype is not None:
            cmd.extend(("-t", vfstype))

        if mntOpts:
            cmd.extend(("-o", mntOpts))

        cmd.extend((self.fs_spec, self.fs_file))

        return self._runcmd(cmd, timeout)

    def _runcmd(self, cmd, timeout):
        isRoot = os.geteuid() == 0
        p = misc.execCmd(cmd, sudo=not isRoot, sync=False)
        if not p.wait(timeout):
            p.kill()
            raise OSError(errno.ETIMEDOUT,
                          "%s operation timed out" % os.path.basename(cmd[0]))

        out, err = p.communicate()
        rc = p.returncode

        if rc == 0:
            return

        raise MountError(rc, ";".join((out, err)))

    def umount(self, force=False, lazy=False, freeloop=False, timeout=None):
        cmd = [constants.EXT_UMOUNT]
        if force:
            cmd.append("-f")

        if lazy:
            cmd.append("-l")

        if freeloop:
            cmd.append("-d")

        cmd.append(self.fs_file)

        return self._runcmd(cmd, timeout)

    def isMounted(self):
        try:
            self.getRecord()
        except OSError:
            return False

        return True

    def getRecord(self):
        # We compare both specs as one of them may match, depending on the
        # system configuration (.e.g. on gfs2 we may match on the realpath).
        if os.path.islink(self.fs_spec):
            fs_specs = self.fs_spec, os.path.realpath(self.fs_spec)
        else:
            fs_specs = self.fs_spec, None

        for record in _iterMountRecords():
            if self.fs_file == record.fs_file and record.fs_spec in fs_specs:
                return record

        raise OSError(errno.ENOENT,
                      "Mount of `%s` at `%s` does not exist" %
                      (self.fs_spec, self.fs_file))

    def __repr__(self):
        return ("<Mount fs_spec='%s' fs_file='%s'>" %
                (self.fs_spec, self.fs_file))
