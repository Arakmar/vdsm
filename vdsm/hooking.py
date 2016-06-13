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

"""
hooking - various stuff useful when writing vdsm hooks

A vm hook expects domain xml in a file named by an environment variable called
_hook_domxml. The hook may change the xml, but the "china store rule" applies -
if you break something, you own it.

before_migration_destination hook receives the xml of the domain from the
source host. The xml of the domain at the destination will differ in various
details.

Return codes:
0 - the hook ended successfully.
1 - the hook failed, other hooks should be processed.
2 - the hook failed, no further hooks should be processed.
>2 - reserved
"""

from vdsm import hooks
import json
import os
import sys
from xml.dom import minidom

from vdsm.commands import execCmd
from vdsm.utils import tobool

# make pyflakes happy
execCmd
tobool


def read_domxml():
    with open(os.environ['_hook_domxml']) as f:
        return minidom.parseString(f.read())


def write_domxml(domxml):
    with open(os.environ['_hook_domxml'], 'w') as f:
        f.write(domxml.toxml(encoding='utf-8'))


def read_json():
    with open(os.environ['_hook_json']) as f:
        return json.loads(f.read())


def write_json(data):
    with open(os.environ['_hook_json'], 'w') as f:
        f.write(json.dumps(data))


def log(message):
    sys.stderr.write(message + '\n')


def exit_hook(message, return_code=2):
    """
    Exit the hook with a given message, which will be printed to the standard
    error stream. A newline will be printed at the end.
    The default return code is 2 for signaling that an error occurred.
    """
    sys.stderr.write(message + "\n")
    sys.exit(return_code)


def load_vm_launch_flags_from_file(vm_id):
    return hooks.load_vm_launch_flags_from_file(vm_id)


def dump_vm_launch_flags_to_file(vm_id, flags):
    hooks.dump_vm_launch_flags_to_file(vm_id, flags)
