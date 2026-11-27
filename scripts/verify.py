#!/usr/bin/env python3
"""Fail-closed archive/stage/FPK validation; compatible with Python 3.10."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import stat
import tarfile
import tempfile


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def digest_file(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def archive_name(name):
    require(isinstance(name, str) and '\\' not in name and '\x00' not in name,
            'Invalid archive path')
    require(not name.startswith('/') and not re.match(r'^[A-Za-z]:', name),
            'Absolute archive path forbidden')
    require('..' not in name.split('/'), 'Archive path traversal forbidden')
    return posixpath.normpath(name)


def validate_members(members, max_bytes=8 * 1024**3):
    """Permit contained relative npm links, never writes through a symlink.

    Validate the entire graph before extraction, including forward references.
    Symlink ancestors of archive members are forbidden even when contained.
    """
    entries = {}
    total = 0
    for member in members:
        name = archive_name(member.name)
        if name == '.':
            require(member.isdir(), 'Archive root must be directory')
            continue
        require(name not in entries, 'Duplicate archive path: ' + name)
        require(member.isdir() or member.isfile() or member.issym(),
                'Hard links and special files forbidden: ' + name)
        require(not member.mode & 0o7000, 'Special permission bits forbidden: ' + name)
        total += member.size
        require(total <= max_bytes, 'Archive exceeds unpacked size limit')
        require(len(entries) < 300000, 'Archive exceeds entry limit')
        entries[name] = member
    links = {}
    for name, member in entries.items():
        parts = name.split('/')
        for i in range(1, len(parts)):
            parent = entries.get('/'.join(parts[:i]))
            require(parent is None or parent.isdir(), 'Non-directory archive ancestor: ' + name)
        if member.issym():
            target = member.linkname
            require(target and not target.startswith('/') and '\\' not in target
                    and '\x00' not in target and not re.match(r'^[A-Za-z]:', target),
                    'Unsafe symlink target: ' + name)
            links[name] = target

    def resolve(name, active):
        parts = []
        for part in name.split('/'):
            if part in ('', '.'):
                continue
            if part == '..':
                require(parts, 'Symlink escapes archive')
                parts.pop()
                continue
            parts.append(part)
            prefix = '/'.join(parts)
            if prefix in links:
                require(prefix not in active, 'Cyclic symlink: ' + prefix)
                target = posixpath.join(posixpath.dirname(prefix), links[prefix])
                parts = resolve(target, active | {prefix}).split('/')
                if parts == ['']:
                    parts = []
        return '/'.join(parts)

    for name in links:
        resolve(name, set())
    return entries


def safe_extract(archive, destination, expected_root=None):
    """Extract without tarfile.extract[all], ownership restoration, or link following."""
    destination = Path(destination)
    require(not destination.is_symlink(), 'Extraction destination is a symlink')
    destination.mkdir(parents=True, exist_ok=True)
    require(not any(destination.iterdir()), 'Extraction destination must be empty')
    with tarfile.open(archive, 'r:*') as source:
        entries = validate_members(source.getmembers())
        if expected_root:
            require(entries and all(n == expected_root or n.startswith(expected_root + '/')
                                    for n in entries), 'Unexpected archive top-level directory')
        dirs = []
        for name, member in entries.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
                dirs.append((target, member.mode & 0o777))
            elif member.isfile():
                with source.extractfile(member) as src, target.open('xb') as out:
                    shutil.copyfileobj(src, out)
                target.chmod(member.mode & 0o777)
        # Links last: no file write can traverse one, even with adversarial ordering.
        for name, member in entries.items():
            if member.issym():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
        for target, mode in sorted(dirs, key=lambda item: len(item[0].parts), reverse=True):
            target.chmod(mode)


def parse_manifest(text):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        require('=' in line, 'Malformed manifest line')
        key, value = (part.strip() for part in line.split('=', 1))
        require(key not in result, 'Duplicate manifest key: ' + key)
        result[key] = value
    return result


def verify_elf(path):
    with Path(path).open('rb') as stream:
        header = stream.read(20)
    require(len(header) == 20 and header[:6] == b'\x7fELF\x02\x01'
            and int.from_bytes(header[18:20], 'little') == 62,
            'Expected Linux x86_64 ELF: ' + str(path))


def verify_stage(stage, metadata=None):
    from PIL import Image
    stage = Path(stage)
    for root, directories, files in os.walk(stage, followlinks=False):
        for name in directories + files:
            path = Path(root) / name
            mode = path.lstat().st_mode
            require(stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode),
                    'Special stage file forbidden')
            require(not mode & 0o7000, 'Special stage permission forbidden')
            if path.is_symlink():
                require(not os.path.isabs(os.readlink(path)), 'Absolute stage link forbidden')
                try:
                    path.resolve().relative_to(stage.resolve())
                except (ValueError, RuntimeError):
                    raise VerificationError('Stage symlink escapes or cycles') from None
                require(path.exists(), 'Dangling stage symlink: ' + str(path.relative_to(stage)))
    required = ['manifest', 'LICENSE', 'app/studio/LICENSE', 'app/build-info.json',
                'config/privilege', 'config/resource', 'app/ui/config',
                'app/bin/service.mjs', 'app/studio/package.json',
                'app/studio/bin/hermes-web-ui.mjs', 'app/studio/dist/server/index.js',
                'app/runtime/node/bin/node', 'app/runtime/node/bin/npm',
                'app/runtime/node/LICENSE', 'app/runtime/node/lib/node_modules/npm/package.json']
    commands = ['main', 'install_init', 'install_callback', 'uninstall_init',
                'uninstall_callback', 'upgrade_init', 'upgrade_callback', 'config_init', 'config_callback']
    required += ['cmd/' + command for command in commands]
    for name in required:
        require((stage / name).is_file(), 'Required file missing: ' + name)
    for name in ['cmd/' + command for command in commands] + ['app/bin/service.mjs', 'app/runtime/node/bin/node']:
        require((stage / name).stat().st_mode & 0o111, 'Required executable bit missing: ' + name)
    manifest = parse_manifest((stage / 'manifest').read_text())
    require(manifest.get('appname') == 'ekko-studio', 'Incorrect FPK appname')
    require(manifest.get('platform') == 'x86' and manifest.get('arch', 'x86_64') == 'x86_64',
            'FPK must target x86_64')
    for key in ('version', 'display_name', 'desc', 'source', 'desktop_applaunchname'):
        require(manifest.get(key), 'Required manifest field missing: ' + key)
    require(manifest['source'] == 'thirdparty', 'Incorrect manifest source')
    documents = {}
    json_paths = ['config/privilege', 'config/resource', 'app/ui/config',
                  'app/build-info.json', 'app/studio/package.json']
    if (stage / 'wizard').exists():
        json_paths += [str(p.relative_to(stage)) for p in (stage / 'wizard').iterdir() if p.is_file()]
    for name in json_paths:
        documents[name] = json.loads((stage / name).read_text())
    ui = documents['app/ui/config']
    require(manifest['desktop_applaunchname'] in ui.get('.url', ui), 'Desktop launch entry missing')
    for name, size in [('ICON.PNG', 64), ('ICON_256.PNG', 256),
                       ('app/ui/images/icon_64.png', 64), ('app/ui/images/icon_256.png', 256)]:
        with Image.open(stage / name) as image:
            require(image.format == 'PNG' and image.size == (size, size), 'Incorrect icon: ' + name)
            image.load()
            # fnOS-style rounded silhouettes need transparent outer corners.
            # Keep the central artwork opaque; a blank/transparent icon is invalid.
            alpha = image.convert('RGBA').getchannel('A')
            center = alpha.crop((size // 4, size // 4, size * 3 // 4, size * 3 // 4))
            require(center.getextrema() == (255, 255),
                    'Transparent app icon center forbidden: ' + name)
    info = documents['app/build-info.json']
    if metadata:
        require(all(info.get(key) == value for key, value in metadata.items()),
                'Stage metadata differs from resolved metadata')
        require(manifest['version'] == metadata['version'], 'FPK version mismatch')
        require(documents['app/studio/package.json']['version'] == metadata['upstream_version'],
                'Upstream package version mismatch')
    verify_elf(stage / 'app/runtime/node/bin/node')
    native = list((stage / 'app/studio/node_modules').rglob('*.node'))
    require(native, 'Official UI native modules missing')
    require(any('node-pty' in str(p) and 'build/Release' in str(p) for p in native),
            'Compiled node-pty missing')
    for path in native:
        # Official dependencies can ship extra platform prebuilds; check the active build.
        if 'build/Release' in str(path) or 'linux-x64' in str(path):
            verify_elf(path)
    require((stage / 'LICENSE').read_bytes() == (stage / 'app/studio/LICENSE').read_bytes(),
            'License copies differ')
    return manifest


def verify_fpk(path, metadata=None):
    """Official fnpack may omit manifest.checksum. Never invent its format.

    The manifest checksum field MUST always equal MD5(app.tgz). An optional
    manifest.checksum is accepted only in the observed plain-MD5 format.
    """
    with tempfile.TemporaryDirectory(prefix='verify-fpk-') as temp:
        outer = Path(temp) / 'outer'
        safe_extract(path, outer)
        require((outer / 'app.tgz').is_file() and (outer / 'manifest').is_file(), 'Incomplete FPK envelope')
        manifest = parse_manifest((outer / 'manifest').read_text())
        actual = digest_file(outer / 'app.tgz', 'md5')
        require(manifest.get('checksum', '').lower() == actual, 'FPK manifest checksum mismatch')
        checksum = outer / 'manifest.checksum'
        if checksum.exists():
            require(checksum.read_text().strip().lower() == actual, 'Unsupported or incorrect manifest.checksum')
        inner = Path(temp) / 'inner'
        safe_extract(outer / 'app.tgz', inner)
        # fnpack emits app contents at root; older versions include app/ itself.
        app = inner / 'app' if (inner / 'app').is_dir() else inner
        require(not (outer / 'app').exists(), 'Unexpected outer app directory')
        shutil.copytree(app, outer / 'app', symlinks=True)
        verify_stage(outer, metadata)
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--stage', type=Path)
    group.add_argument('--fpk', type=Path)
    parser.add_argument('archives', nargs='*', type=Path, help='One or more FPK files')
    parser.add_argument('--metadata', type=Path)
    args = parser.parse_args()
    if (args.stage or args.fpk) and args.archives:
        parser.error('Use positional FPK paths OR --stage/--fpk')
    if not (args.stage or args.fpk or args.archives):
        parser.error('Provide --stage, --fpk, or one or more FPK paths')
    try:
        metadata = json.loads(args.metadata.read_text()) if args.metadata else None
        if args.stage:
            verify_stage(args.stage, metadata)
        else:
            for archive in ([args.fpk] if args.fpk else args.archives):
                verify_fpk(archive, metadata)
    except (ValueError, OSError, tarfile.TarError) as error:
        parser.exit(1, 'Verification failed: ' + str(error) + '\n')
    print('Verification passed')


if __name__ == '__main__':
    main()
