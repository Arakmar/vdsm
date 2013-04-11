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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
import misc
from vdsm import constants


def fuser(path, mountPoint=False):
    cmd = [constants.EXT_FUSER]
    if mountPoint:
        cmd.append("-m")

    cmd.append(path)
    (rc, out, err) = misc.execCmd(cmd, raw=True, sudo=False)
    if rc != 0:
        return []

    return [int(pid) for pid in out.split()]
