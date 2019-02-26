#
# Copyright 2016 Red Hat, Inc.
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

import logging
import time

import six

from vdsm.storage import constants as sc
from vdsm.storage import exception


class VolumeMetadata(object):

    log = logging.getLogger('storage.VolumeMetadata')

    def __init__(self, domain, image, puuid, size, format, type, voltype,
                 disktype, description="", legality=sc.ILLEGAL_VOL, ctime=None,
                 generation=sc.DEFAULT_GENERATION):
        if not isinstance(size, six.integer_types):
            raise AssertionError("Invalid value for 'size': {!r}".format(size))
        if ctime is not None and not isinstance(ctime, int):
            raise AssertionError(
                "Invalid value for 'ctime': {!r}".format(ctime))
        if not isinstance(generation, int):
            raise AssertionError(
                "Invalid value for 'generation': {!r}".format(generation))

        # Storage domain UUID
        self.domain = domain
        # Image UUID
        self.image = image
        # UUID of the parent volume or BLANK_UUID
        self.puuid = puuid
        # Volume size in blocks
        self.size = size
        # Format (RAW or COW)
        self.format = format
        # Allocation policy (PREALLOCATED or SPARSE)
        self.type = type
        # Relationship to other volumes (LEAF, INTERNAL or SHARED)
        self.voltype = voltype
        # Intended usage of this volume (unused)
        self.disktype = disktype
        # Free-form description and may be used to store extra metadata
        self.description = description
        # Indicates if the volume contents should be considered valid
        self.legality = legality
        # Volume creation time (in seconds since the epoch)
        self.ctime = int(time.time()) if ctime is None else ctime
        # Generation increments each time certain operations complete
        self.generation = generation

    @classmethod
    def from_lines(cls, lines):
        md = {}
        for line in lines:
            if line.startswith("EOF"):
                break
            if '=' not in line:
                continue
            key, value = line.split('=', 1)
            md[key.strip()] = value.strip()

        try:
            return cls(domain=md[sc.DOMAIN],
                       image=md[sc.IMAGE],
                       puuid=md[sc.PUUID],
                       size=int(md[sc.SIZE]),
                       format=md[sc.FORMAT],
                       type=md[sc.TYPE],
                       voltype=md[sc.VOLTYPE],
                       disktype=md[sc.DISKTYPE],
                       description=md[sc.DESCRIPTION],
                       legality=md[sc.LEGALITY],
                       ctime=int(md[sc.CTIME]),
                       # generation was added to the set of metadata keys well
                       # after the above fields.  Therefore, it may not exist
                       # on storage for pre-existing volumes.  In that case we
                       # report a default value of 0 which will be written to
                       # the volume metadata on the next metadata change.
                       generation=int(md.get(sc.GENERATION,
                                             sc.DEFAULT_GENERATION)))
        except KeyError as e:
            raise exception.MetaDataKeyNotFoundError(
                "Missing metadata key: %s: found: %s" % (e, md))

    @property
    def description(self):
        return self._description

    @description.setter
    def description(self, desc):
        self._description = self.validate_description(desc)

    @classmethod
    def validate_description(cls, desc):
        desc = str(desc)
        # We cannot fail when the description is too long, since we must
        # support older engine that may send such values, or old disks
        # with long description.
        if len(desc) > sc.DESCRIPTION_SIZE:
            cls.log.warning("Description is too long, truncating to %d bytes",
                            sc.DESCRIPTION_SIZE)
            desc = desc[:sc.DESCRIPTION_SIZE]
        return desc

    def storage_format(self, domain_version, **overrides):
        """
        Format metadata string in storage format.

        VolumeMetadata is quite restrictive and doesn't allows
        you to make an invalid metadata, but sometimes, for example
        for a format conversion, you need some additional fields to
        be written to the storage. Those fields can be added using
        overrides dict.

        Raises MetadataOverflowError if formatted metadata is too long.

        NOTE: Not used yet! We need to drop legacy_info() and pass
        VolumeMetadata instance instead of a dict to use this code.
        """

        info = dict(self.iteritems())
        if domain_version < 5:
            # Always zero on pre v5 domains
            # We need to keep MTIME available on pre v5
            # domains, as other code is expecting that
            # field to exists and will fail without it.
            info[sc.MTIME] = 0

        info.update(overrides)

        keys = sorted(info.keys())
        lines = ["%s=%s\n" % (key, info[key]) for key in keys]
        lines.append("EOF\n")
        data = "".join(lines)
        if len(data) > sc.METADATA_SIZE:
            raise exception.MetadataOverflowError(data)
        return data

    # Three defs below allow us to imitate a dictionary
    # So intstead of providing a method to return a dictionary
    # with values, we return self and mimick dict behaviour.
    # In the fieldmap we keep mapping between metadata
    # field name and our internal field names
    _fieldmap = {
        sc.FORMAT: 'format',
        sc.TYPE: 'type',
        sc.VOLTYPE: 'voltype',
        sc.DISKTYPE: 'disktype',
        sc.SIZE: 'size',
        sc.CTIME: 'ctime',
        sc.DOMAIN: 'domain',
        sc.IMAGE: 'image',
        sc.DESCRIPTION: 'description',
        sc.PUUID: 'puuid',
        sc.LEGALITY: 'legality',
        sc.GENERATION: 'generation',
    }

    def __getitem__(self, item):
        try:
            value = getattr(self, self._fieldmap[item])
        except AttributeError:
            raise KeyError(item)

        # Some fields needs to be converted to string
        if item in (sc.SIZE, sc.CTIME):
            value = str(value)
        return value

    def __setitem__(self, item, value):
        setattr(self, self._fieldmap[item], value)

    def get(self, item, default=None):
        try:
            return self[item]
        except KeyError:
            return default

    def iteritems(self):
        for item in self._fieldmap:
            yield (item, self[item])
