#!/usr/bin/env python
# Copyright 2015 Red Hat, Inc.
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
import sys
import traceback

import hooking

# TODO: move required modules into vdsm/lib
sys.path.append('/usr/share/vdsm')
import supervdsm


def main():
    setup_nets_config = hooking.read_json()

    in_rollback = setup_nets_config['request']['options'].get('_inRollback')

    if in_rollback:
        hooking.log('Configuration failed with _inRollback=True.')
    else:
        hooking.log('Configuration failed. At this point, non-OVS rollback '
                    'should be done. Executing OVS rollback.')
        supervdsm.getProxy().setupNetworks(
            {}, {}, {'connectivityCheck': False, '_inRollback': True,
                     '_inOVSRollback': True})


if __name__ == '__main__':
    try:
        main()
    except:
        hooking.exit_hook(traceback.format_exc())
