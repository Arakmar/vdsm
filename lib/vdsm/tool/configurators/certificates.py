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
import sys

from vdsm.config import config

from . import YES, NO
from .. validate_ovirt_certs import validate_ovirt_certs
from ... constants import P_VDSM_EXEC, SYSCONF_PATH
from ... commands import execCmd
from ... utils import isOvirtNode

PKI_DIR = os.path.join(SYSCONF_PATH, 'pki/vdsm')
CA_FILE = os.path.join(PKI_DIR, 'certs/cacert.pem')
CERT_FILE = os.path.join(PKI_DIR, 'certs/vdsmcert.pem')
KEY_FILE = os.path.join(PKI_DIR, 'keys/vdsmkey.pem')


def validate():
    return _certsExist()


def _exec_vdsm_gencerts():
    rc, out, err = execCmd(
        (
            os.path.join(
                P_VDSM_EXEC,
                'vdsm-gencerts.sh'
            ),
            CA_FILE,
            KEY_FILE,
            CERT_FILE,
        ),
        raw=True,
    )
    sys.stdout.write(out)
    sys.stderr.write(err)
    if rc != 0:
        raise RuntimeError("Failed to perform vdsm-gencerts action.")


def configure():
    _exec_vdsm_gencerts()
    if isOvirtNode():
        validate_ovirt_certs()


def isconfigured():
    return YES if _certsExist() else NO


def _certsExist():
    return not config.getboolean('vars', 'ssl') or\
        os.path.isfile(CERT_FILE)
