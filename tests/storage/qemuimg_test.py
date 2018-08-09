#
# Copyright 2014-2017 Red Hat, Inc.
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

import io
import json
import os
import pprint
from functools import partial

from monkeypatch import MonkeyPatch, MonkeyPatchScope

from . import qemuio

from testlib import VdsmTestCase as TestCaseBase
from testlib import permutations, expandPermutations
from testlib import make_config
from testlib import namedTemporaryDir
from vdsm.common import cmdutils
from vdsm.common import commands
from vdsm.common import constants
from testlib import temporaryPath
from vdsm.common import exception
from vdsm.storage import qemuimg

QEMU_IMG = qemuimg._qemuimg.cmd

CONFIG = make_config([('irs', 'qcow2_compat', '0.10')])


def fake_json_call(data, cmd, **kw):
    return 0, json.dumps(data).encode("utf-8"), []


@expandPermutations
class GeneralTests(TestCaseBase):
    @permutations((("0.10", True), ("1.1", True), ("10.1", False)))
    def test_supports_compat(self, compat, result):
        self.assertEqual(result, qemuimg.supports_compat(compat))


@expandPermutations
class InfoTests(TestCaseBase):
    CLUSTER_SIZE = 65536

    def _fake_info(self):
        return {
            "virtual-size": 1048576,
            "filename": "leaf.img",
            "cluster-size": self.CLUSTER_SIZE,
            "format": "qcow2",
            "actual-size": 200704,
            "format-specific": {
                "type": "qcow2",
                "data": {
                    "compat": "1.1",
                    "lazy-refcounts": False,
                    "refcount-bits": 16,
                    "corrupt": False
                }
            },
            "backing-filename": "/var/tmp/test.img",
            "dirty-flag": False
        }

    def test_info(self):
        with namedTemporaryDir() as tmpdir:
            base_path = os.path.join(tmpdir, 'base.img')
            leaf_path = os.path.join(tmpdir, 'leaf.img')
            size = 1048576
            leaf_fmt = qemuimg.FORMAT.QCOW2
            with MonkeyPatchScope([(qemuimg, 'config', CONFIG)]):
                op = qemuimg.create(base_path,
                                    size=size,
                                    format=qemuimg.FORMAT.RAW)
                op.run()
                op = qemuimg.create(leaf_path,
                                    format=leaf_fmt,
                                    backing=base_path)
                op.run()

            info = qemuimg.info(leaf_path)
            self.assertEqual(leaf_fmt, info['format'])
            self.assertEqual(size, info['virtualsize'])
            self.assertEqual(self.CLUSTER_SIZE, info['clustersize'])
            self.assertEqual(base_path, info['backingfile'])
            self.assertEqual('0.10', info['compat'])

    @permutations([
        # unsafe
        (True,),
        (False,),
    ])
    def test_unsafe_info(self, unsafe):
        with namedTemporaryDir() as tmpdir:
            img = os.path.join(tmpdir, 'img.img')
            size = 1048576
            op = qemuimg.create(img, size=size, format=qemuimg.FORMAT.QCOW2)
            op.run()
            info = qemuimg.info(img, unsafe=unsafe)
            self.assertEqual(size, info['virtualsize'])

    def test_parse_error(self):
        def call(cmd, **kw):
            out = b"image: leaf.img\ninvalid file format line"
            return 0, out, ""

        with MonkeyPatchScope([(commands, "execCmd", call)]):
            self.assertRaises(cmdutils.Error, qemuimg.info, 'leaf.img')

    @permutations((('format',), ('virtual-size',)))
    def test_missing_required_field_raises(self, field):
        data = self._fake_info()
        del data[field]
        with MonkeyPatchScope([(commands, "execCmd",
                                partial(fake_json_call, data))]):
            self.assertRaises(cmdutils.Error, qemuimg.info, 'leaf.img')

    def test_missing_compat_for_qcow2_raises(self):
        data = self._fake_info()
        del data['format-specific']['data']['compat']
        with MonkeyPatchScope([(commands, "execCmd",
                                partial(fake_json_call, data))]):
            self.assertRaises(cmdutils.Error, qemuimg.info, 'leaf.img')

    @permutations((
        ('backing-filename', 'backingfile'),
        ('cluster-size', 'clustersize'),
    ))
    def test_optional_fields(self, qemu_field, info_field):
        data = self._fake_info()
        del data[qemu_field]
        with MonkeyPatchScope([(commands, "execCmd",
                                partial(fake_json_call, data))]):
            info = qemuimg.info('unused')
            self.assertNotIn(info_field, info)

    def test_compat_reported_for_qcow2_only(self):
        data = {
            "virtual-size": 1048576,
            "filename": "raw.img",
            "format": "raw",
            "actual-size": 0,
            "dirty-flag": False
        }
        with MonkeyPatchScope([(commands, "execCmd",
                                partial(fake_json_call, data))]):
            info = qemuimg.info('unused')
            self.assertNotIn('compat', info)

    def test_untrusted_image(self):
        with namedTemporaryDir() as tmpdir:
            img = os.path.join(tmpdir, 'untrusted.img')
            size = 500 * 1024**3
            op = qemuimg.create(img, size=size, format=qemuimg.FORMAT.QCOW2)
            op.run()
            info = qemuimg.info(img, trusted_image=False)
            self.assertEqual(size, info['virtualsize'])

    def test_untrusted_image_call(self):
        command = []

        def call(cmd, *args, **kwargs):
            command.extend(cmd)
            out = json.dumps(self._fake_info()).encode("utf-8")
            return 0, out, b""

        with MonkeyPatchScope([(commands, "execCmd", call)]):
            qemuimg.info('unused', trusted_image=False)

        assert command[:3] == [constants.EXT_PRLIMIT,
                               '--cpu=30',
                               '--as=1073741824']


@expandPermutations
class CreateTests(TestCaseBase):
    @permutations((
        (qemuimg.FORMAT.RAW, qemuimg.PREALLOCATION.OFF, 0),
        (qemuimg.FORMAT.RAW, qemuimg.PREALLOCATION.FALLOC, 16 * 1024 * 1024),
        (qemuimg.FORMAT.RAW, qemuimg.PREALLOCATION.FULL, 16 * 1024 * 1024)
    ))
    def test_allocate(self, image_format, allocation_mode, allocated_bytes):
        size = 16 * 1024 * 1024
        with temporaryPath() as image:
            op = qemuimg.create(image,
                                size=size,
                                format=image_format,
                                preallocation=allocation_mode)
            op.run()
            allocated = os.stat(image).st_blocks * 512
            self.assertEqual(allocated, allocated_bytes)

    def test_no_format(self):
        size = 4096
        with namedTemporaryDir() as tmpdir:
            image = os.path.join(tmpdir, "image")
            op = qemuimg.create(image, size=size)
            op.run()

            info = qemuimg.info(image)
            self.assertEqual(info['format'], qemuimg.FORMAT.RAW)
            self.assertEqual(info['virtualsize'], size)

    def test_zero_size(self):
        with namedTemporaryDir() as tmpdir:
            image = os.path.join(tmpdir, "image")
            op = qemuimg.create(image, size=0)
            op.run()

            info = qemuimg.info(image)
            self.assertEqual(info['format'], qemuimg.FORMAT.RAW)
            self.assertEqual(info['virtualsize'], 0)

    def test_qcow2_compat(self):
        with namedTemporaryDir() as tmpdir:
            image = os.path.join(tmpdir, "image")
            size = 1024 * 1024 * 1024 * 10  # 10 GB
            op = qemuimg.create(image, format='qcow2', size=size)
            op.run()

            info = qemuimg.info(image)
            self.assertEqual(info['format'], qemuimg.FORMAT.QCOW2)
            self.assertEqual(info['compat'], "0.10")
            self.assertEqual(info['virtualsize'], size)

    def test_qcow2_compat_version3(self):
        with namedTemporaryDir() as tmpdir:
            image = os.path.join(tmpdir, "image")
            size = 1024 * 1024 * 1024 * 10  # 10 GB
            op = qemuimg.create(image, format='qcow2',
                                qcow2Compat='1.1', size=size)
            op.run()

            info = qemuimg.info(image)
            self.assertEqual(info['format'], qemuimg.FORMAT.QCOW2)
            self.assertEqual(info['compat'], "1.1")
            self.assertEqual(info['virtualsize'], size)

    def test_qcow2_compat_invalid(self):
        with self.assertRaises(ValueError):
            qemuimg.create('image', format='qcow2', qcow2Compat='1.11')

    def test_invalid_config(self):
        config = make_config([('irs', 'qcow2_compat', '1.2')])
        with MonkeyPatchScope([(qemuimg, 'config', config)]):
            with self.assertRaises(exception.InvalidConfiguration):
                qemuimg.create('image', format='qcow2')

    @MonkeyPatch(qemuimg, 'config', CONFIG)
    def test_unsafe_create_volume(self):
        with namedTemporaryDir() as tmpdir:
            path = os.path.join(tmpdir, 'test.qcow2')
            # Using unsafe=True to verify that it is possible to create an
            # image based on a non-existing backing file, like an inactive LV.
            qemuimg.create(path, size=1048576, format=qemuimg.FORMAT.QCOW2,
                           backing='no-such-file', unsafe=True)


class ConvertTests(TestCaseBase):

    def test_no_format(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', '-T', 'none',
                        'src', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, 'ProgressCommand', convert)]):
            qemuimg.convert('src', 'dst')

    def test_qcow2_compat(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', '-T', 'none',
                        'src', '-O', 'qcow2', '-o', 'compat=0.10', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, 'config', CONFIG),
                               (qemuimg, 'ProgressCommand', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2')

    def test_qcow2_compat_version3(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', '-T', 'none',
                        'src', '-O', 'qcow2', '-o', 'compat=1.1', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, 'config', CONFIG),
                               (qemuimg, 'ProgressCommand', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2',
                            dstQcow2Compat='1.1')

    def test_qcow2_no_backing_file(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', '-T', 'none',
                        'src', '-O', 'qcow2', '-o', 'compat=0.10', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, 'config', CONFIG),
                               (qemuimg, 'ProgressCommand', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2')

    def test_qcow2_backing_file(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', '-T', 'none',
                        'src', '-O', 'qcow2', '-o',
                        'compat=0.10,backing_file=bak', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, 'config', CONFIG),
                               (qemuimg, 'ProgressCommand', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2',
                            backing='bak')

    def test_qcow2_backing_format(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', '-T', 'none',
                        'src', '-O', 'qcow2', '-o', 'compat=0.10', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, 'config', CONFIG),
                               (qemuimg, 'ProgressCommand', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2',
                            backingFormat='qcow2')

    def test_qcow2_backing_file_and_format(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', '-T', 'none',
                        'src', '-O', 'qcow2', '-o',
                        'compat=0.10,backing_file=bak,backing_fmt=qcow2',
                        'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, 'config', CONFIG),
                               (qemuimg, 'ProgressCommand', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2',
                            backing='bak', backingFormat='qcow2')

    def test_qcow2_compat_invalid(self):
        with self.assertRaises(ValueError):
            qemuimg.convert('image', 'dst', dstFormat='qcow2',
                            backing='bak', backingFormat='qcow2',
                            dstQcow2Compat='1.11')


@expandPermutations
class TestConvertPreallocation(TestCaseBase):

    @permutations([
        # preallocation, virtual_size, actual_size
        (None, 10 * 1024**2, 0),
        (qemuimg.PREALLOCATION.OFF, 10 * 1024**2, 0),
        (qemuimg.PREALLOCATION.FALLOC, 10 * 1024**2, 10 * 1024**2),
        (qemuimg.PREALLOCATION.FULL, 10 * 1024**2, 10 * 1024**2),
    ])
    def test_raw_to_raw(self, preallocation, virtual_size, actual_size):
        with namedTemporaryDir() as tmpdir:
            src = os.path.join(tmpdir, 'src')
            dst = os.path.join(tmpdir, 'dst')

            with io.open(src, "wb") as f:
                f.truncate(virtual_size)

            op = qemuimg.convert(src, dst, srcFormat="raw", dstFormat="raw",
                                 preallocation=preallocation)
            op.run()

            stat = os.stat(dst)
            self.assertEqual(stat.st_size, virtual_size)
            self.assertEqual(stat.st_blocks * 512, actual_size)

    @permutations([
        # preallocation, virtual_size, actual_size
        (None, 10 * 1024**2, 0),
        (qemuimg.PREALLOCATION.OFF, 10 * 1024**2, 0),
        (qemuimg.PREALLOCATION.FALLOC, 10 * 1024**2, 10 * 1024**2),
        (qemuimg.PREALLOCATION.FULL, 10 * 1024**2, 10 * 1024**2),
        (qemuimg.PREALLOCATION.FULL, 10 * 1024**2, 10 * 1024**2),
    ])
    def test_qcow2_to_raw(self, preallocation, virtual_size, actual_size):
        with namedTemporaryDir() as tmpdir:
            src = os.path.join(tmpdir, 'src')
            dst = os.path.join(tmpdir, 'dst')

            op = qemuimg.create(src, size=virtual_size, format="qcow2")
            op.run()

            op = qemuimg.convert(src, dst, srcFormat="qcow2", dstFormat="raw",
                                 preallocation=preallocation)
            op.run()

            stat = os.stat(dst)
            self.assertEqual(stat.st_size, virtual_size)
            self.assertEqual(stat.st_blocks * 512, actual_size)

    def test_raw_invalid_preallocation(self):
        with self.assertRaises(ValueError):
            qemuimg.convert(
                'src', 'dst', dstFormat="raw",
                preallocation=qemuimg.PREALLOCATION.METADATA)


class CheckTests(TestCaseBase):

    @MonkeyPatch(qemuimg, 'config', CONFIG)
    def test_check(self):
        with namedTemporaryDir() as tmpdir:
            path = os.path.join(tmpdir, 'test.qcow2')
            op = qemuimg.create(path,
                                size=1048576,
                                format=qemuimg.FORMAT.QCOW2)
            op.run()
            info = qemuimg.check(path)
            # The exact value depends on qcow2 internals
            self.assertEqual(int, type(info['offset']))

    def test_offset_no_match(self):
        with MonkeyPatchScope([(commands, "execCmd",
                                partial(fake_json_call, {}))]):
            self.assertRaises(cmdutils.Error, qemuimg.check, 'unused')

    def test_parse_error(self):
        def call(cmd, **kw):
            out = b"image: leaf.img\ninvalid file format line"
            return 0, out, ""

        with MonkeyPatchScope([(commands, "execCmd", call)]):
            self.assertRaises(cmdutils.Error, qemuimg.check, 'unused')


class TestProgressCommand(TestCaseBase):

    def test_failure(self):
        p = qemuimg.ProgressCommand(['false'])
        self.assertRaises(cmdutils.Error, p.run)

    def test_no_progress(self):
        p = qemuimg.ProgressCommand(['true'])
        p.run()
        self.assertEqual(p.progress, 0.0)

    def test_progress(self):
        p = qemuimg.ProgressCommand([
            'echo', "-n",
            "    (0.00/100%)\r    (50.00/100%)\r    (100.00/100%)\r"
        ])
        p.run()
        self.assertEqual(p.progress, 100.0)

    def test_partial_progress(self):
        p = qemuimg.ProgressCommand([])
        out = bytearray()
        out += b"    (42.00/100%)\r"
        p._update_progress(out)
        self.assertEqual(p.progress, 42.0)
        self.assertEqual(out, b"")
        out += b"    (43.00/"
        p._update_progress(out)
        self.assertEqual(p.progress, 42.0)
        self.assertEqual(out, b"    (43.00/")
        out += b"100%)\r"
        p._update_progress(out)
        self.assertEqual(p.progress, 43.0)
        self.assertEqual(out, b"")

    def test_use_last_progress(self):
        p = qemuimg.ProgressCommand([])
        out = bytearray()
        out += b"    (11.00/100%)\r    (12.00/100%)\r    (13.00/100%)\r"
        p._update_progress(out)
        self.assertEqual(p.progress, 13.0)
        self.assertEqual(out, b"")

    def test_unexpected_output(self):
        p = qemuimg.ProgressCommand([])
        out = bytearray()
        out += b"    (42.00/100%)\r"
        p._update_progress(out)
        out += b"invalid progress\r"
        with self.assertRaises(ValueError):
            p._update_progress(out)
        self.assertEqual(p.progress, 42.0)


@expandPermutations
class TestCommit(TestCaseBase):

    @permutations([
        # qcow2_compat, base, top, use_base
        # Merging internal volume into its parent volume in raw format
        ("1.1", 0, 1, False),
        ("1.1", 0, 1, True),
        ("0.10", 0, 1, False),
        ("0.10", 0, 1, True),
        # Merging internal volume into its parent volume in cow format
        ("1.1", 1, 2, True),
        ("0.10", 1, 2, True),
        # Merging a subchain
        ("1.1", 1, 3, True),
        ("0.10", 1, 3, True),
        # Merging the entire chain into the base
        ("1.1", 0, 3, True),
        ("0.10", 0, 3, True)
    ])
    def test_commit(self, qcow2_compat, base, top, use_base):
        size = 1048576
        with namedTemporaryDir() as tmpdir:
            chain = []
            parent = None
            # Create a chain of 4 volumes.
            for i in range(4):
                vol = os.path.join(tmpdir, "vol%d.img" % i)
                format = (qemuimg.FORMAT.RAW if i == 0 else
                          qemuimg.FORMAT.QCOW2)
                make_image(vol, size, format, i, qcow2_compat, parent)
                orig_offset = qemuimg.check(vol)["offset"] if i > 0 else None
                chain.append((vol, orig_offset))
                parent = vol

            base_vol = chain[base][0]
            top_vol = chain[top][0]
            op = qemuimg.commit(top_vol,
                                topFormat=qemuimg.FORMAT.QCOW2,
                                base=base_vol if use_base else None)
            op.run()

            base_fmt = (qemuimg.FORMAT.RAW if base == 0 else
                        qemuimg.FORMAT.QCOW2)
            for i in range(base, top + 1):
                offset = i * 1024
                pattern = 0xf0 + i
                # The base volume must have the data from all the volumes
                # merged into it.
                qemuio.verify_pattern(
                    base_vol,
                    base_fmt,
                    offset=offset,
                    len=1024,
                    pattern=pattern)

                if i > base:
                    # internal and top volumes should keep the data, we
                    # may want to wipe this data when deleting the volumes
                    # later.
                    vol, orig_offset = chain[i]
                    actual_offset = qemuimg.check(vol)["offset"]
                    self.assertEqual(actual_offset, orig_offset)

    def test_commit_progress(self):
        with namedTemporaryDir() as tmpdir:
            size = 1048576
            base = os.path.join(tmpdir, "base.img")
            make_image(base, size, qemuimg.FORMAT.RAW, 0, "1.1")

            top = os.path.join(tmpdir, "top.img")
            make_image(top, size, qemuimg.FORMAT.QCOW2, 1, "1.1", base)

            op = qemuimg.commit(top, topFormat=qemuimg.FORMAT.QCOW2)
            op.run()
            self.assertEqual(100, op.progress)


@expandPermutations
class TestMap(TestCaseBase):

    # We test only qcow2 images since this is the only use case that we need
    # now.  Testing raw images is tricky, the result depends on the file system
    # supporting SEEK_DATA and SEEK_HOLE. If these are supported, empty image
    # will be seen as one block with data=False. If not supported (seen on
    # travis-ci), empty image will be seen as one block with data=True.
    FORMAT = qemuimg.FORMAT.QCOW2

    @permutations([["0.10"], ["1.1"]])
    def test_empty_image(self, qcow2_compat):
        with namedTemporaryDir() as tmpdir:
            size = 1048576
            image = os.path.join(tmpdir, "base.img")
            op = qemuimg.create(image, size=size, format=self.FORMAT,
                                qcow2Compat=qcow2_compat)
            op.run()

            expected = [
                # single run - empty
                {
                    "start": 0,
                    "length": size,
                    "data": False,
                    "zero": True,
                },
            ]

            self.check_map(qemuimg.map(image), expected)

    @permutations([
        # offset, length, expected_length, expected_start, qcow2_compat
        (64 * 1024, 4 * 1024, 65536, "0.10"),
        (64 * 1024, 4 * 1024, 65536, "1.1"),
        (64 * 1024, 72 * 1024, 131072, "0.10"),
        (64 * 1024, 72 * 1024, 131072, "1.1"),
    ])
    def test_one_block(self, offset, length, expected_length, qcow2_compat):
        with namedTemporaryDir() as tmpdir:
            size = 1048576
            image = os.path.join(tmpdir, "base.img")
            op = qemuimg.create(image, size=size, format=self.FORMAT,
                                qcow2Compat=qcow2_compat)
            op.run()

            qemuio.write_pattern(
                image,
                self.FORMAT,
                offset=offset,
                len=length,
                pattern=0xf0)

            expected = [
                # run 1 - empty
                {
                    "start": 0,
                    "length": offset,
                    "data": False,
                    "zero": True,
                },
                # run 2 - data
                {
                    "start": offset,
                    "length": expected_length,
                    "data": True,
                    "zero": False,
                },
                # run 3 - empty
                {
                    "start": offset + expected_length,
                    "length": size - offset - expected_length,
                    "data": False,
                    "zero": True,
                },
            ]

            self.check_map(qemuimg.map(image), expected)

    def check_map(self, actual, expected):
        if len(expected) != len(actual):
            msg = "Length mismatch: %d != %d" % (len(expected), len(actual))
            raise MapMismatch(msg, expected, actual)

        for actual_run, expected_run in zip(actual, expected):
            for key in expected_run:
                if expected_run[key] != actual_run[key]:
                    msg = "Value mismatch for %r: %s != %s" % (
                        key, expected_run, actual_run)
                    raise MapMismatch(msg, expected, actual)


@expandPermutations
class TestAmend(TestCaseBase):

    @permutations([
        # qcow2_compat, desired_qcow2_compat
        ("0.10", "1.1"),
        ("0.10", "0.10"),
        ("1.1", "1.1"),
    ])
    @MonkeyPatch(qemuimg, 'config', CONFIG)
    def test_empty_image(self, qcow2_compat, desired_qcow2_compat):
        with namedTemporaryDir() as tmpdir:
            base_path = os.path.join(tmpdir, 'base.img')
            leaf_path = os.path.join(tmpdir, 'leaf.img')
            size = 1048576
            op_base = qemuimg.create(base_path, size=size,
                                     format=qemuimg.FORMAT.RAW)
            op_base.run()
            op_leaf = qemuimg.create(leaf_path, format=qemuimg.FORMAT.QCOW2,
                                     backing=base_path)
            op_leaf.run()
            qemuimg.amend(leaf_path, desired_qcow2_compat)
            self.assertEqual(qemuimg.info(leaf_path)['compat'],
                             desired_qcow2_compat)


def make_image(path, size, format, index, qcow2_compat, backing=None):
    op = qemuimg.create(path, size=size, format=format,
                        qcow2Compat=qcow2_compat,
                        backing=backing)
    op.run()
    offset = index * 1024
    qemuio.write_pattern(
        path,
        format,
        offset=offset,
        len=1024,
        pattern=0xf0 + index)


class MapMismatch(AssertionError):

    def __init__(self, message, expected, actual):
        self.message = message
        self.expected = expected
        self.actual = actual

    def __str__(self):
        text = self.message + "\n"
        text += "\n"
        text += "Expected map:\n"
        text += pprint.pformat(self.expected) + "\n"
        text += "\n"
        text += "Actual map:\n"
        text += pprint.pformat(self.actual) + "\n"
        return text
