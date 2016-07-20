#
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import json
import os
from functools import partial

from monkeypatch import MonkeyPatch, MonkeyPatchScope
from testlib import VdsmTestCase as TestCaseBase
from testlib import permutations, expandPermutations
from testlib import make_config
from testlib import namedTemporaryDir
from vdsm import qemuimg
from vdsm import commands
from vdsm import exception

QEMU_IMG = qemuimg._qemuimg.cmd

CONFIG = make_config([('irs', 'qcow2_compat', '0.10')])


class CommandTests(TestCaseBase):

    def supported(self, command, result):
        def check(arg):
            self.assertEqual(command, arg)
            return result
        return check


def fake_json_call(data, cmd, **kw):
    return 0, json.dumps(data), []


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
                qemuimg.create(base_path, size=size, format=qemuimg.FORMAT.RAW)
                qemuimg.create(leaf_path, format=leaf_fmt, backing=base_path)

            info = qemuimg.info(leaf_path)
            self.assertEqual(leaf_fmt, info['format'])
            self.assertEqual(size, info['virtualsize'])
            self.assertEqual(self.CLUSTER_SIZE, info['clustersize'])
            self.assertEqual(base_path, info['backingfile'])
            self.assertEqual('0.10', info['compat'])

    def test_parse_error(self):
        def call(cmd, **kw):
            out = "image: leaf.img\ninvalid file format line"
            return 0, out, ""

        with MonkeyPatchScope([(commands, "execCmd", call)]):
            self.assertRaises(qemuimg.QImgError, qemuimg.info, 'leaf.img')

    @permutations((('format',), ('virtual-size',)))
    def test_missing_required_field_raises(self, field):
        data = self._fake_info()
        del data[field]
        with MonkeyPatchScope([(commands, "execCmd",
                                partial(fake_json_call, data))]):
            self.assertRaises(qemuimg.QImgError, qemuimg.info, 'leaf.img')

    def test_missing_compat_for_qcow2_raises(self):
        data = self._fake_info()
        del data['format-specific']['data']['compat']
        with MonkeyPatchScope([(commands, "execCmd",
                                partial(fake_json_call, data))]):
            self.assertRaises(qemuimg.QImgError, qemuimg.info, 'leaf.img')

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


class CreateTests(CommandTests):

    def test_no_format(self):
        def create(cmd, **kw):
            expected = [QEMU_IMG, 'create', 'image']
            self.assertEqual(cmd, expected)
            return 0, '', ''

        with MonkeyPatchScope([(commands, "execCmd", create)]):
            qemuimg.create('image')

    def test_zero_size(self):
        def create(cmd, **kw):
            expected = [QEMU_IMG, 'create', 'image', '0']
            self.assertEqual(cmd, expected)
            return 0, '', ''

        with MonkeyPatchScope([(commands, "execCmd", create)]):
            qemuimg.create('image', size=0)

    def test_qcow2_compat_unsupported(self):
        def create(cmd, **kw):
            expected = [QEMU_IMG, 'create', '-f', 'qcow2', 'image']
            self.assertEqual(cmd, expected)
            return 0, '', ''

        with MonkeyPatchScope([(qemuimg, '_supports_qcow2_compat',
                                self.supported('create', False)),
                               (commands, 'execCmd', create)]):
            qemuimg.create('image', format='qcow2')

    def test_qcow2_compat_supported(self):

        def create(cmd, **kw):
            expected = [QEMU_IMG, 'create', '-f', 'qcow2', '-o', 'compat=0.10',
                        'image']
            self.assertEqual(cmd, expected)
            return 0, '', ''

        with MonkeyPatchScope([(qemuimg, '_supports_qcow2_compat',
                                self.supported('create', True)),
                               (qemuimg, 'config', CONFIG),
                               (commands, 'execCmd', create)]):
            qemuimg.create('image', format='qcow2')

    def test_invalid_config(self):
        config = make_config([('irs', 'qcow2_compat', '1.2')])
        with MonkeyPatchScope([(qemuimg, '_supports_qcow2_compat',
                                self.supported('create', True)),
                               (qemuimg, 'config', config)]):
            with self.assertRaises(exception.InvalidConfiguration):
                qemuimg.create('image', format='qcow2')


class ConvertTests(CommandTests):

    def test_no_format(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', 'src', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, 'QemuImgOperation', convert),
                               (qemuimg, '_supports_src_cache',
                                self.supported('convert', False))]):
            qemuimg.convert('src', 'dst')

    def test_qcow2_compat_unsupported(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', 'src', '-O',
                        'qcow2', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, '_supports_qcow2_compat',
                                self.supported('convert', False)),
                               (qemuimg, '_supports_src_cache',
                                self.supported('convert', False)),
                               (qemuimg, 'QemuImgOperation', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2')

    def test_qcow2_compat_supported(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', 'src', '-O',
                        'qcow2', '-o', 'compat=0.10', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, '_supports_qcow2_compat',
                                self.supported('convert', True)),
                               (qemuimg, '_supports_src_cache',
                                self.supported('convert', False)),
                               (qemuimg, 'config', CONFIG),
                               (qemuimg, 'QemuImgOperation', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2')

    def test_qcow2_no_backing_file(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', 'src',
                        '-O', 'qcow2', '-o', 'compat=0.10', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, '_supports_qcow2_compat',
                                self.supported('convert', True)),
                               (qemuimg, '_supports_src_cache',
                                self.supported('convert', False)),
                               (qemuimg, 'config', CONFIG),
                               (qemuimg, 'QemuImgOperation', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2')

    def test_qcow2_backing_file(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', 'src',
                        '-O', 'qcow2', '-o', 'compat=0.10,backing_file=bak',
                        'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, '_supports_qcow2_compat',
                                self.supported('convert', True)),
                               (qemuimg, '_supports_src_cache',
                                self.supported('convert', False)),
                               (qemuimg, 'config', CONFIG),
                               (qemuimg, 'QemuImgOperation', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2',
                            backing='bak')

    def test_qcow2_backing_format(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', 'src',
                        '-O', 'qcow2', '-o', 'compat=0.10', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, '_supports_qcow2_compat',
                                self.supported('convert', True)),
                               (qemuimg, '_supports_src_cache',
                                self.supported('convert', False)),
                               (qemuimg, 'config', CONFIG),
                               (qemuimg, 'QemuImgOperation', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2',
                            backingFormat='qcow2')

    def test_qcow2_backing_file_and_format(self):
        def convert(cmd, **kw):
            expected = [QEMU_IMG, 'convert', '-p', '-t', 'none', 'src',
                        '-O', 'qcow2', '-o', 'compat=0.10,backing_file=bak,'
                        'backing_fmt=qcow2', 'dst']
            self.assertEqual(cmd, expected)

        with MonkeyPatchScope([(qemuimg, '_supports_qcow2_compat',
                                self.supported('convert', True)),
                               (qemuimg, '_supports_src_cache',
                                self.supported('convert', False)),
                               (qemuimg, 'config', CONFIG),
                               (qemuimg, 'QemuImgOperation', convert)]):
            qemuimg.convert('src', 'dst', dstFormat='qcow2',
                            backing='bak', backingFormat='qcow2')


def qcow2_compat_supported(cmd, **kw):
    return 0, 'Supported options:\ncompat ...\n', ''


def qcow2_compat_unsupported(cmd, **kw):
    return 0, 'Supported options:\nsize ...\n', ''


class SupportsQcow2ComaptTests(TestCaseBase):

    @MonkeyPatch(commands, 'execCmd', qcow2_compat_supported)
    def test_create_supported(self, **kw):
        self.assertTrue(qemuimg._supports_qcow2_compat('create'))

    @MonkeyPatch(commands, 'execCmd', qcow2_compat_unsupported)
    def test_create_unsupported(self, **kw):
        self.assertFalse(qemuimg._supports_qcow2_compat('create'))

    @MonkeyPatch(commands, 'execCmd', qcow2_compat_supported)
    def test_convert_supported(self, **kw):
        self.assertTrue(qemuimg._supports_qcow2_compat('convert'))

    @MonkeyPatch(commands, 'execCmd', qcow2_compat_unsupported)
    def test_convert_unsupported(self, **kw):
        self.assertFalse(qemuimg._supports_qcow2_compat('convert'))


def src_cache_supported(cmd, **kw):
    out = """
  compare [-f fmt] [-F fmt] [-T src_cache] ...
  convert [-c] [-p] [-q] [-n] [-f fmt] [-t cache] [-T src_cache] ...
  rebase [-q] [-f fmt] [-t cache] [-T src_cache] [-p] ...
  resize [-q] filename [+ | -]size ...
"""

    return 0, out, ''


def rebase_src_cache_unsupported(cmd, **kw):
    out = """
  compare [-f fmt] [-F fmt] [-T src_cache] ...
  convert [-c] [-p] [-q] [-n] [-f fmt] [-t cache] [-T src_cache] ...
  rebase [-q] [-f fmt] [-t cache] [-p] ...
  resize [-q] filename [+ | -]size ...
"""

    return 0, out, ''


def convert_src_cache_unsupported(cmd, **kw):
    out = """
  compare [-f fmt] [-F fmt] [-T src_cache] ...
  convert [-c] [-p] [-q] [-n] [-f fmt] [-t cache] ...
  rebase [-q] [-f fmt] [-t cache] [-T src_cache] [-p] ...
  resize [-q] filename [+ | -]size ...
"""

    return 0, out, ''


class SupportsSrcCacheTests(TestCaseBase):

    def tearDown(self):
        qemuimg._supports_src_cache.invalidate()

    @MonkeyPatch(commands, 'execCmd', src_cache_supported)
    def test_rebase_supported(self, **kw):
        self.assertTrue(qemuimg._supports_src_cache('rebase'))

    @MonkeyPatch(commands, 'execCmd', rebase_src_cache_unsupported)
    def test_rebase_unsupported(self, **kw):
        self.assertFalse(qemuimg._supports_src_cache('rebase'))

    @MonkeyPatch(commands, 'execCmd', src_cache_supported)
    def test_convert_supported(self, **kw):
        self.assertTrue(qemuimg._supports_src_cache('convert'))

    @MonkeyPatch(commands, 'execCmd', convert_src_cache_unsupported)
    def test_convert_unsupported(self, **kw):
        self.assertFalse(qemuimg._supports_src_cache('convert'))


class CheckTests(TestCaseBase):

    @MonkeyPatch(qemuimg, 'config', CONFIG)
    def test_check(self):
        with namedTemporaryDir() as tmpdir:
            path = os.path.join(tmpdir, 'test.qcow2')
            qemuimg.create(path, size=1048576, format=qemuimg.FORMAT.QCOW2)
            info = qemuimg.check(path)
            # The exact value depends on qcow2 internals
            self.assertEqual(int, type(info['offset']))

    def test_offset_no_match(self):
        with MonkeyPatchScope([(commands, "execCmd",
                                partial(fake_json_call, {}))]):
            self.assertRaises(qemuimg.QImgError, qemuimg.check, 'unused')

    def test_parse_error(self):
        def call(cmd, **kw):
            out = "image: leaf.img\ninvalid file format line"
            return 0, out, ""

        with MonkeyPatchScope([(commands, "execCmd", call)]):
            self.assertRaises(qemuimg.QImgError, qemuimg.check, 'unused')


@expandPermutations
class QemuImgProgressTests(TestCaseBase):
    PROGRESS_FORMAT = "    (%.2f/100%%)\r"

    @staticmethod
    def _progress_iterator():
        for value in xrange(0, 10000, 1):
            yield value / 100.0

    def test_failure(self):
        p = qemuimg.QemuImgOperation(['false'])
        self.assertRaises(qemuimg.QImgError, p.wait)

    def test_progress_simple(self):
        p = qemuimg.QemuImgOperation(['true'])

        for progress in self._progress_iterator():
            p._recvstdout(self.PROGRESS_FORMAT % progress)
            self.assertEquals(p.progress, progress)

        p.wait()
        self.assertEquals(p.finished, True)

    @permutations([
        (("    (1/100%)\r", "    (2/100%)\r"), (1, 2)),
        (("    (1/10", "0%)\r    (2/10"),      (0, 1)),
        (("    (1/10", "0%)\r    (2/100%)\r"), (0, 2)),
    ])
    def test_partial(self, output_list, progress_list):
        p = qemuimg.QemuImgOperation(['true'])

        for output, progress in zip(output_list, progress_list):
            p._recvstdout(output)
            self.assertEquals(p.progress, progress)
        p.wait()
        self.assertEquals(p.finished, True)

    def test_progress_batch(self):
        p = qemuimg.QemuImgOperation(['true'])

        p._recvstdout(
            (self.PROGRESS_FORMAT % 10.00) +
            (self.PROGRESS_FORMAT % 25.00) +
            (self.PROGRESS_FORMAT % 33.33))

        self.assertEquals(p.progress, 33.33)

        p.wait()
        self.assertEquals(p.finished, True)

    def test_unexpected_output(self):
        p = qemuimg.QemuImgOperation(['true'])

        self.assertRaises(ValueError, p._recvstdout, 'Hello World\r')

        p._recvstdout('Hello ')
        self.assertRaises(ValueError, p._recvstdout, 'World\r')

        p.wait()
        self.assertEquals(p.finished, True)
