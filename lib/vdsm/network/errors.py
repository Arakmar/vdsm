#
# Copyright 2011-2014 Red Hat, Inc.
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
ERR_OK = 0
ERR_BAD_PARAMS = 21
ERR_BAD_ADDR = 22
ERR_BAD_NIC = 23
ERR_USED_NIC = 24
ERR_BAD_BONDING = 25
ERR_BAD_VLAN = 26
ERR_BAD_BRIDGE = 27
ERR_USED_BRIDGE = 28
ERR_FAILED_IFUP = 29
ERR_FAILED_IFDOWN = 30
ERR_USED_BOND = 31
ERR_LOST_CONNECTION = 10    # noConPeer


class ConfigNetworkError(Exception):
    def __init__(self, errCode, message):
        self.errCode = errCode
        self.message = message
        super(ConfigNetworkError, self).__init__(errCode, message)


class RollbackIncomplete(Exception):
    """
    This exception is raised in order to signal API.Global that a call to
    setupNetworks has failed and there are leftovers that need to be cleaned
    up.
    Note that it is never raised by the default ifcfg configurator.
    """
    pass
