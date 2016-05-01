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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
import os
import re
import signal

from . import utils
from . config import config

_qemuimg = utils.CommandPath("qemu-img",
                             "/usr/bin/qemu-img",)  # Fedora, EL6


class FORMAT:
    QCOW2 = "qcow2"
    QCOW = "qcow"
    QED = "qed"
    RAW = "raw"
    VMDK = "vmdk"

__iregex = {
    'format': re.compile("^file format: (?P<value>\w+)$"),
    'virtualsize': re.compile("^virtual size: "
                              "[\d.]+[KMGT] \((?P<value>\d+) bytes\)$"),
    'clustersize': re.compile("^cluster_size: (?P<value>\d+)$"),
    'backingfile': re.compile("^backing file: (?P<value>.+) \(actual path"),
    'offset': re.compile("^Image end offset: (?P<value>\d+)$"),
}

# The first row of qemu-img info output where optional fields may appear
_INFO_OPTFIELDS_STARTIDX = 4

# The first row of qemu-img check output where the 'offset' may appear
_CHECK_OPTFIELDS_STARTIDX = 1


class _RegexSearchError(Exception):
    pass


def __iregexSearch(pattern, text):
    m = __iregex[pattern].search(text)
    if m is None:
        raise _RegexSearchError()
    return m.group("value")


class QImgError(Exception):
    def __init__(self, ecode, stdout, stderr, message=None):
        self.ecode = ecode
        self.stdout = stdout
        self.stderr = stderr
        self.message = message

    def __str__(self):
        return "ecode=%s, stdout=%s, stderr=%s, message=%s" % (
            self.ecode, self.stdout, self.stderr, self.message)


def info(image, format=None):
    cmd = [_qemuimg.cmd, "info"]

    if format:
        cmd.extend(("-f", format))

    cmd.append(image)
    rc, out, err = utils.execCmd(cmd, deathSignal=signal.SIGKILL)

    if rc != 0:
        raise QImgError(rc, out, err)

    try:
        info = {
            'format': __iregexSearch("format", out[1]),
            'virtualsize': int(__iregexSearch("virtualsize", out[2])),
        }
    except _RegexSearchError:
        raise QImgError(rc, out, err, "unable to parse qemu-img info output")

    # Scan for optional fields in the output
    row = _INFO_OPTFIELDS_STARTIDX
    for field, filterFn in (('clustersize', int), ('backingfile', str)):
        try:
            info[field] = filterFn(__iregexSearch(field, out[row]))
        except (_RegexSearchError, IndexError):
            pass
        else:
            row = row + 1

    return info


def create(image, size=None, format=None, backing=None, backingFormat=None):
    cmd = [_qemuimg.cmd, "create"]
    cwdPath = None

    if format:
        cmd.extend(("-f", format))
        if format == FORMAT.QCOW2 and _supports_qcow2_compat('create'):
            cmd.extend(('-o', 'compat=' + config.get('irs', 'qcow2_compat')))

    if backing:
        if not os.path.isabs(backing):
            cwdPath = os.path.dirname(image)
        cmd.extend(("-b", backing))

    if backingFormat:
        cmd.extend(("-F", backingFormat))

    cmd.append(image)

    if size is not None:
        cmd.append(str(size))

    rc, out, err = utils.execCmd(cmd, cwd=cwdPath, deathSignal=signal.SIGKILL)

    if rc != 0:
        raise QImgError(rc, out, err)


def check(image, format=None):
    cmd = [_qemuimg.cmd, "check"]

    if format:
        cmd.extend(("-f", format))

    cmd.append(image)
    rc, out, err = utils.execCmd(cmd, deathSignal=signal.SIGKILL)

    # FIXME: handle different error codes and raise errors accordingly
    if rc != 0:
        raise QImgError(rc, out, err)
    # Scan for 'offset' in the output
    for row in range(_CHECK_OPTFIELDS_STARTIDX, len(out)):
        try:
            check = {
                'offset': int(__iregexSearch("offset", out[row]))
            }
            return check
        except _RegexSearchError:
            pass
        except:
            break
    raise QImgError(rc, out, err, "unable to parse qemu-img check output")


def convert(srcImage, dstImage, stop, srcFormat=None, dstFormat=None,
            backing=None, backingFormat=None):
    cmd = [_qemuimg.cmd, "convert", "-t", "none"]
    options = []
    cwdPath = None

    if _supports_src_cache('convert'):
        cmd.extend(("-T", "none"))

    if srcFormat:
        cmd.extend(("-f", srcFormat))

    cmd.append(srcImage)

    if dstFormat:
        cmd.extend(("-O", dstFormat))
        if dstFormat == FORMAT.QCOW2 and _supports_qcow2_compat('convert'):
            options.append('compat=' + config.get('irs', 'qcow2_compat'))

    if backing:
        if not os.path.isabs(backing):
            cwdPath = os.path.dirname(srcImage)

        options.append('backing_file=' + str(backing))

        if backingFormat:
            options.append('backing_fmt=' + str(backingFormat))

    if options:
        cmd.extend(('-o', ','.join(options)))

    cmd.append(dstImage)

    (rc, out, err) = utils.watchCmd(
        cmd, cwd=cwdPath, stop=stop, nice=utils.NICENESS.HIGH,
        ioclass=utils.IOCLASS.IDLE)

    if rc != 0:
        raise QImgError(rc, out, err)

    return (rc, out, err)


def resize(image, newSize, format=None):
    cmd = [_qemuimg.cmd, "resize"]

    if format:
        cmd.extend(("-f", format))

    cmd.extend((image, str(newSize)))
    rc, out, err = utils.execCmd(cmd, deathSignal=signal.SIGKILL)

    if rc != 0:
        raise QImgError(rc, out, err)


def rebase(image, backing, format=None, backingFormat=None, unsafe=False,
           stop=None):
    cmd = [_qemuimg.cmd, "rebase", "-t", "none"]

    if _supports_src_cache('rebase'):
        cmd.extend(("-T", "none"))

    if unsafe:
        cmd.extend(("-u",))

    if format:
        cmd.extend(("-f", format))

    if backingFormat:
        cmd.extend(("-F", backingFormat))

    cmd.extend(("-b", backing, image))

    cwdPath = None if os.path.isabs(backing) else os.path.dirname(image)
    rc, out, err = utils.watchCmd(
        cmd, cwd=cwdPath, stop=stop, nice=utils.NICENESS.HIGH,
        ioclass=utils.IOCLASS.IDLE)

    if rc != 0:
        raise QImgError(rc, out, err)


# Testing capabilities

def _supports_qcow2_compat(command):
    """
    qemu-img "create" and "convert" commands support a "compat" option in
    recent versions. This will run the specified command using the "-o ?"
    option to find if "compat" option is available.

    Raises KeyError if called with another command.

    TODO: Remove this when qemu versions providing the "compat" option are
    available on all platforms.
    """
    # Older qemu-img requires all filenames although unneeded
    args = {"create": ("-f", ("/dev/null",)),
            "convert": ("-O", ("/dev/null", "/dev/null"))}
    flag, dummy_files = args[command]

    cmd = [_qemuimg.cmd, command, flag, FORMAT.QCOW2, "-o", "?"]
    cmd.extend(dummy_files)

    rc, out, err = utils.execCmd(cmd, raw=True)

    if rc != 0:
        raise QImgError(rc, out, err)

    # Supported options:
    # compat           Compatibility level (0.10 or 1.1)

    return '\ncompat ' in out


@utils.memoized
def _supports_src_cache(command):
    """
    The "-T" option specifies the cache mode that should be used with the
    source file. This will check if "-T" option is available, aiming to set it
    to "none", avoiding the use of cache memory (BZ#1138690).
    """
    # REQUIRED_FOR: FEDORA 20 (no qemu-img with -T support)
    cmd = [_qemuimg.cmd, "--help"]
    rc, out, err = utils.execCmd(cmd, raw=True)

    # REQUIRED_FOR: EL6 (--help returns 1)
    if rc not in (0, 1):
        raise QImgError(rc, out, err)

    # Line to match:
    #   convert [-c] [-p] [-q] [-n] [-f fmt] [-t cache] [-T src_cache]...
    pattern = r"\n +%s .*\[-T src_cache\]" % command
    return re.search(pattern, out) is not None
