# Copyright 2014 Red Hat, Inc.
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
import shutil
import sys
import tempfile
import time

from .import \
    YES, \
    NO
from .. import service
from ... import utils
from ... import constants


_MPATH_CONF = "/etc/multipath.conf"

_MPATH_CONF_TAG = "# VDSM REVISION 1.2"

_MPATH_CONF_DATA = """\
%(current_tag)s

defaults {
    polling_interval            5
    no_path_retry               fail
    user_friendly_names         no
    flush_on_last_del           yes
    fast_io_fail_tmo            5
    dev_loss_tmo                30
    max_fds                     4096
}

# Remove devices entries when overrides section is available.
devices {
    device {
        vendor                  "HITACHI"
        product                 "DF.*"
    }
    device {
        vendor                  "COMPELNT"
        product                 "Compellent Vol"
        no_path_retry           fail
    }
    device {
        # multipath.conf.default
        vendor                  "DGC"
        product                 ".*"
        product_blacklist       "LUNZ"
        path_grouping_policy    "group_by_prio"
        path_checker            "emc_clariion"
        hardware_handler        "1 emc"
        prio                    "emc"
        failback                immediate
        rr_weight               "uniform"
        # vdsm required configuration
        no_path_retry           fail
    }
}

# Enable when this section is available on all supported platforms.
# Options defined here override device specific options embedded into
# multipathd.
#
# overrides {
#      no_path_retry           fail
# }

""" % {"current_tag": _MPATH_CONF_TAG}

_MAX_CONF_COPIES = 5

# conf file configured by vdsm should contain a tag
# in form of "RHEV REVISION X.Y"
_OLD_TAGS = ["# RHAT REVISION 0.2", "# RHEV REVISION 0.3",
             "# RHEV REVISION 0.4", "# RHEV REVISION 0.5",
             "# RHEV REVISION 0.6", "# RHEV REVISION 0.7",
             "# RHEV REVISION 0.8", "# RHEV REVISION 0.9",
             "# RHEV REVISION 1.0", "# RHEV REVISION 1.1"]

# Having the PRIVATE_TAG in the conf file means
# vdsm-tool should never change the conf file
# even when using the --force flag
_OLD_PRIVATE_TAG = "# RHEV PRIVATE"
_MPATH_CONF_PRIVATE_TAG = "# VDSM PRIVATE"

# If multipathd is up, it will be reloaded after configuration,
# or started before vdsm starts, so service should not be stopped
# during configuration.
services = []


def configure():
    """
    Set up the multipath daemon configuration to the known and
    supported state. The original configuration, if any, is saved
    """

    if os.path.exists(_MPATH_CONF):
        backup = _MPATH_CONF + '.' + time.strftime("%Y%m%d%H%M")
        shutil.copyfile(_MPATH_CONF, backup)
        utils.persist(backup)

    with tempfile.NamedTemporaryFile() as f:
        f.write(_MPATH_CONF_DATA)
        f.flush()
        cmd = [constants.EXT_CP, f.name,
               _MPATH_CONF]
        rc, out, err = utils.execCmd(cmd)

        if rc != 0:
            raise RuntimeError("Failed to perform Multipath config.")
    utils.persist(_MPATH_CONF)

    # Flush all unused multipath device maps
    utils.execCmd([constants.EXT_MULTIPATH, "-F"])

    try:
        service.service_reload("multipathd")
    except service.ServiceOperationError:
        status = service.service_status("multipathd", False)
        if status == 0:
            raise


def isconfigured():
    """
    Check the multipath daemon configuration. The configuration file
    /etc/multipath.conf should contain a tag in form
    "RHEV REVISION X.Y" for this check to succeed.
    If the tag above is followed by tag "RHEV PRIVATE" the configuration
    should be preserved at all cost.
    """

    if os.path.exists(_MPATH_CONF):
        first = second = ''
        with open(_MPATH_CONF) as f:
            mpathconf = [x.strip("\n") for x in f.readlines()]
        try:
            first = mpathconf[0]
            second = mpathconf[1]
        except IndexError:
            pass
        if _MPATH_CONF_PRIVATE_TAG in second or _OLD_PRIVATE_TAG in second:
            sys.stdout.write("Manual override for multipath.conf detected"
                             " - preserving current configuration\n")
            if _MPATH_CONF_TAG not in first:
                sys.stdout.write("This manual override for multipath.conf "
                                 "was based on downrevved template. "
                                 "You are strongly advised to "
                                 "contact your support representatives\n")
            return YES

        if _MPATH_CONF_TAG in first:
            sys.stdout.write("Current revision of multipath.conf detected,"
                             " preserving\n")
            return YES

        for tag in _OLD_TAGS:
            if tag in first:
                sys.stdout.write("Downrev multipath.conf detected, "
                                 "upgrade required\n")
                return NO

    sys.stdout.write("multipath requires configuration\n")
    return NO
