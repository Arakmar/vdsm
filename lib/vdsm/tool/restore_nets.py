# Copyright 2013 Red Hat, Inc.
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

import os
import sys

from .. import utils
from . import expose, ExtraArgsError
from ..constants import P_VDSM


@expose('restore-nets')
def restore_command(*args):
    """
    restore-nets
    Restores the networks to what was previously persisted via vdsm.
    """
    if len(args) > 1:
        raise ExtraArgsError()
    restore()


def restore():
    rc, out, err = utils.execCmd(
        [os.path.join(P_VDSM, 'vdsm-restore-net-config'), '--force'], raw=True)
    sys.stdout.write(out)
    sys.stderr.write(err)
    if rc != 0:
        raise EnvironmentError('Failed to restore the persisted networks')
