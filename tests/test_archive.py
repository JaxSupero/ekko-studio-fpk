#!/usr/bin/env python3
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from verify import (VerificationError, parse_manifest, safe_extract, validate_members,
                    digest_file, verify_fpk, verify_stage)
from build import crosscheck_release, read_metadata, repair_fnpack_links
import hashlib
import json
import os
import shutil
import subprocess
import struct
import zlib
from unittest import mock


def tar_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, kind, target in entries:
            info = tarfile.TarInfo(name)
            if kind == 'dir':
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == 'sym':
                info.type = tarfile.SYMTYPE
                info.linkname = target
                archive.addfile(info)
            else:
                data = target
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


class ArchiveSafetyTests(unittest.TestCase):
    def extract(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        archive = Path(tmp.name) / 'input.tar.gz'
        archive.write_bytes(data)
        destination = Path(tmp.name) / 'out'
        safe_extract(archive, destination)
        return destination

    def test_reject_parent_and_absolute_paths(self):
        for name in ('../escape', '/etc/passwd', 'C:/escape', 'a/../../escape'):
            with self.subTest(name=name), self.assertRaises(VerificationError):
                self.extract(tar_bytes([(name, 'file', b'x')]))

    def test_reject_escaping_absolute_and_cyclic_links(self):
        for link in ('../../outside', '/etc/passwd', 'a/loop'):
            entries = [('a', 'dir', ''), ('a/link', 'sym', link)]
            if link == 'a/loop':
                entries = [('a/loop', 'sym', 'loop')]
            with self.subTest(link=link), self.assertRaises(VerificationError):
                self.extract(tar_bytes(entries))

    def test_reject_file_written_through_symlink(self):
        entries = [('root', 'sym', 'inside'), ('root/payload', 'file', b'x')]
        with self.assertRaises(VerificationError):
            self.extract(tar_bytes(entries))

    def test_preserve_normal_node_modules_relative_links(self):
        entries = [('node_modules', 'dir', ''), ('node_modules/pkg', 'dir', ''),
                   ('packages', 'dir', ''), ('packages/pkg', 'dir', ''),
                   ('node_modules/pkg-link', 'sym', '../packages/pkg'),
                   ('packages/pkg/index.js', 'file', b'ok')]
        root = self.extract(tar_bytes(entries))
        self.assertEqual((root / 'node_modules/pkg-link').resolve(),
                         (root / 'packages/pkg').resolve())
        self.assertEqual((root / 'node_modules/pkg-link/index.js').read_bytes(), b'ok')

    def test_reject_hardlinks_special_files_and_permission_bits(self):
        link = tarfile.TarInfo('hard')
        link.type = tarfile.LNKTYPE
        link.linkname = 'file'
        for member in (link,):
            with self.subTest(member=member.name), self.assertRaises(VerificationError):
                validate_members([member])
        special = tarfile.TarInfo('fifo')
        special.type = tarfile.FIFOTYPE
        with self.assertRaises(VerificationError):
            validate_members([special])
        privileged = tarfile.TarInfo('setuid')
        privileged.mode = 0o4755
        with self.assertRaises(VerificationError):
            validate_members([privileged])

    def test_manifest_checksum_is_md5_of_inner_archive(self):
        import hashlib
        content = b'app archive bytes'
        value = hashlib.md5(content).hexdigest()
        self.assertEqual(parse_manifest('checksum = ' + value)['checksum'], value)
        self.assertNotEqual(value, hashlib.md5(b'changed').hexdigest())

    def test_official_manifest_checksums_crosscheck(self):
        asset = {'name': 'hermes-web-ui-0.8.31.tar.gz', 'size': 100, 'sha256': 'a' * 64}
        metadata = {'asset': asset, 'upstream_version': '0.8.31'}
        manifest = {'schema': 1, 'webUiVersion': '0.8.31', 'asset': dict(asset)}
        crosscheck_release(metadata, manifest, asset['sha256'] + '  ' + asset['name'] + '\n')
        with self.assertRaises(VerificationError):
            crosscheck_release(metadata, manifest, 'b' * 64 + '  ' + asset['name'] + '\n')
        manifest['asset']['size'] = 101
        with self.assertRaises(VerificationError):
            crosscheck_release(metadata, manifest, asset['sha256'] + '  ' + asset['name'] + '\n')

    def test_manifest_duplicate_keys_rejected(self):
        with self.assertRaises(VerificationError):
            parse_manifest('version = 1\nversion = 2\n')


def png_bytes(size):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\xff' * size * 4) * size)) + chunk(b'IEND', b''))


def synthetic_stage(root):
    """No native execution or downloads; x86_64 ELF header is deliberately synthetic."""
    root.mkdir(parents=True)
    contents = {
        'manifest': 'appname = ekko-studio\nversion = 0.8.31-1\nplatform = x86\nsource = thirdparty\n'
                    'display_name = Ekko Studio\ndesc = Test\ndesktop_uidir = ui\n'
                    'desktop_applaunchname = ekko-studio.Application\nos_min_version = 0.9.27\n',
        'config/privilege': '{"defaults":{"run-as":"package"},"username":"ekko-studio","groupname":"ekko-studio"}',
        'config/resource': '{}', 'app/ui/config': '{".url":{"ekko-studio.Application":{"title":"Ekko",'
            '"type":"url","protocol":"http","port":"8648","url":"/","icon":"images/icon_{0}.png"}}}',
        'app/build-info.json': '{"version":"0.8.31-1","upstream_version":"0.8.31"}',
        'app/studio/package.json': '{"version":"0.8.31"}',
        'app/studio/bin/hermes-web-ui.mjs': '// test\n',
        'app/studio/dist/server/index.js': '// test\n',
        'app/bin/service.mjs': '#!/usr/bin/env node\n',
        'LICENSE': 'Original license\n', 'app/studio/LICENSE': 'Original license\n',
        'app/runtime/node/LICENSE': 'Node license\n',
        'app/runtime/node/lib/node_modules/npm/package.json': '{"name":"npm"}',
        'app/runtime/node/lib/node_modules/npm/bin/npm-cli.js': '#!/usr/bin/env node\n',
    }
    for command in ('main', 'install_init', 'install_callback', 'uninstall_init',
                    'uninstall_callback', 'upgrade_init', 'upgrade_callback', 'config_init', 'config_callback'):
        contents['cmd/' + command] = '#!/bin/sh\nexit 0\n'
    for name, data in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        path.chmod(0o755 if name.startswith(('cmd/', 'app/bin/')) else 0o644)
    header = b'\x7fELF\x02\x01' + bytes(12) + b'\x3e\x00'
    for name in ('app/runtime/node/bin/node', 'app/studio/node_modules/node-pty/build/Release/pty.node'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(header)
        path.chmod(0o755)
    (root / 'app/runtime/node/bin/npm').symlink_to('../lib/node_modules/npm/bin/npm-cli.js')
    for name, size in [('ICON.PNG', 64), ('ICON_256.PNG', 256),
                       ('app/ui/images/icon_64.png', 64), ('app/ui/images/icon_256.png', 256)]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes(size))
    return root


class FpkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = synthetic_stage(self.root / 'stage')

    def envelope(self, checksum=None, checksum_file=None):
        app = self.root / 'app.tgz'
        with tarfile.open(app, 'w:gz', dereference=False) as archive:
            for path in sorted((self.stage / 'app').iterdir()):
                archive.add(path, arcname=path.name)
        value = digest_file(app, 'md5') if checksum is None else checksum
        manifest = (self.stage / 'manifest').read_text() + 'checksum = ' + value + '\n'
        fpk = self.root / 'test.fpk'
        with tarfile.open(fpk, 'w:gz', dereference=False) as archive:
            for path in sorted(self.stage.iterdir()):
                if path.name not in ('app', 'manifest'):
                    archive.add(path, arcname=path.name)
            for name, data in [('manifest', manifest.encode()), ('app.tgz', app.read_bytes())]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            if checksum_file is not None:
                data = (checksum_file or value).encode()
                info = tarfile.TarInfo('manifest.checksum')
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return fpk

    def test_complete_fpk_without_checksum_file(self):
        verify_fpk(self.envelope())

    def test_complete_fpk_with_plain_md5_file(self):
        verify_fpk(self.envelope(checksum_file=''))

    def test_bad_manifest_checksum_rejected(self):
        with self.assertRaisesRegex(VerificationError, 'manifest checksum mismatch'):
            verify_fpk(self.envelope(checksum='0' * 32))

    def test_bad_checksum_file_rejected(self):
        with self.assertRaisesRegex(VerificationError, 'manifest.checksum'):
            verify_fpk(self.envelope(checksum_file='1' * 32))

    def test_wrong_target_elf_rejected(self):
        path = self.stage / 'app/runtime/node/bin/node'
        data = bytearray(path.read_bytes())
        data[18:20] = b'\xb7\x00'  # ARM64
        path.write_bytes(data)
        with self.assertRaisesRegex(VerificationError, 'x86_64 ELF'):
            verify_stage(self.stage)

    def test_native_executable_permissions_preserved(self):
        archive = self.root / 'native.tar.gz'
        info = tarfile.TarInfo('pty.node')
        info.mode = 0o755
        info.size = 2
        with tarfile.open(archive, 'w:gz') as stream:
            stream.addfile(info, io.BytesIO(b'ok'))
        safe_extract(archive, self.root / 'native')
        self.assertEqual((self.root / 'native/pty.node').stat().st_mode & 0o777, 0o755)

    def test_official_fnpack_envelope_if_available(self):
        executable = os.environ.get('FNPACK_TEST_BINARY')
        if not executable:
            self.skipTest('Set FNPACK_TEST_BINARY to run the official fnpack integration check')
        subprocess.run([str(Path(executable).absolute()), 'build'], cwd=self.stage, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        fpk = self.stage / 'ekko-studio.fpk'
        with tarfile.open(fpk) as outer:
            manifest = parse_manifest(outer.extractfile('manifest').read().decode())
            app = outer.extractfile('app.tgz').read()
            self.assertEqual(manifest['checksum'], hashlib.md5(app).hexdigest())
            with tarfile.open(fileobj=io.BytesIO(app)) as inner:
                npm = inner.getmember('runtime/node/bin/npm')
                self.assertTrue(npm.issym())
                self.assertEqual(npm.linkname, 'npm')
        with self.assertRaisesRegex(VerificationError, 'Cyclic symlink'):
            verify_fpk(fpk)
        verify_stage(self.stage)
        repair_fnpack_links(fpk, self.stage)
        with tarfile.open(fpk) as outer:
            with tarfile.open(fileobj=io.BytesIO(outer.extractfile('app.tgz').read())) as inner:
                self.assertEqual(inner.getmember('runtime/node/bin/npm').linkname,
                                 '../lib/node_modules/npm/bin/npm-cli.js')
        verify_fpk(fpk)


if __name__ == '__main__':
    unittest.main()
