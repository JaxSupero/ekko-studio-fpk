#!/usr/bin/env python3
"""Prepare verified official artifacts, then package ONLY using official fnpack."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from verify import (VerificationError, digest_file, require, safe_extract,
                    verify_fpk, verify_stage)

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'EKKOLearnAI/ekko-studio'
ASSET_KEYS = ('asset', 'manifest_asset', 'checksums_asset')


def validate_url(url, hosts=None):
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme == 'https' and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment,
            'Only credential-free HTTPS URLs are accepted')
    if hosts:
        require(parsed.hostname in hosts, 'Unexpected download host')
    return parsed


def read_metadata(path):
    """Allowlist public provenance fields; secrets/extra keys cannot enter the FPK."""
    raw = json.loads(Path(path).read_text())
    keys = ('tag', 'version', 'upstream_version', 'source_commit', 'node_version',
            'fnpack_version', 'repository')
    data = {key: raw[key] for key in keys}
    require(data['repository'] == REPOSITORY, 'Unexpected upstream repository')
    for key in ('upstream_version', 'node_version', 'fnpack_version'):
        require(re.fullmatch(r'\d+\.\d+\.\d+', data[key]), 'Invalid ' + key)
    require(re.fullmatch(re.escape(data['upstream_version']) + r'-[1-9]\d*', data['version']),
            'Invalid FPK version')
    require(data['tag'] == 'v' + data['upstream_version'], 'Release tag mismatch')
    require(re.fullmatch(r'[0-9a-f]{40}', data['source_commit']), 'A fixed source commit is required')
    name = 'hermes-web-ui-' + data['upstream_version']
    names = (name + '.tar.gz', name + '.json', name + '.tar.gz.sha256')
    for key, expected_name in zip(ASSET_KEYS, names):
        source = raw[key]
        asset = {field: source[field] for field in ('name', 'url', 'sha256', 'size')}
        require(asset['name'] == expected_name, 'Unexpected official asset name')
        digest = asset['sha256'].removeprefix('sha256:').lower()
        require(re.fullmatch(r'[0-9a-f]{64}', digest), 'GitHub SHA256 asset digest is required')
        asset['sha256'] = digest
        require(type(asset['size']) is int and asset['size'] > 0, 'Invalid asset size')
        parsed = validate_url(asset['url'], {'github.com'})
        expected_path = '/' + REPOSITORY + '/releases/download/' + data['tag'] + '/' + asset['name']
        require(urllib.parse.unquote(parsed.path) == expected_path, 'Asset must belong to the fixed upstream release')
        data[key] = asset
    if 'wrapper_commit' in raw:
        require(raw['wrapper_commit'] == 'local' or re.fullmatch(r'[0-9a-f]{40}', raw['wrapper_commit']),
                'Invalid wrapper commit')
        data['wrapper_commit'] = raw['wrapper_commit']
    if 'hermes_agent_policy' in raw:
        require(raw['hermes_agent_policy'] == 'upstream-managed; not independently pinned or bundled',
                'Invalid Hermes Agent policy')
        data['hermes_agent_policy'] = raw['hermes_agent_policy']
    return data


def download(url, target, sha256=None, size=None, max_bytes=2 * 1024**3):
    """No environment dumps, auth in URLs, or token-forwarding redirects."""
    validate_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'ekko-studio-fpk-builder/1'})
    h = hashlib.sha256()
    count = 0
    try:
        with urllib.request.urlopen(request, timeout=120) as source, Path(target).open('wb') as out:
            require(source.geturl().startswith('https://'), 'Insecure download redirect')
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                require(count <= max_bytes and (size is None or count <= size), 'Download exceeds expected size')
                h.update(chunk)
                out.write(chunk)
    except (urllib.error.URLError, TimeoutError):
        raise VerificationError('HTTPS download failed for ' + urllib.parse.urlsplit(url).hostname) from None
    require(size is None or count == size, 'Download size mismatch')
    actual = h.hexdigest()
    require(sha256 is None or actual == sha256, 'Download SHA256 mismatch')
    return actual


def parse_checksums(text):
    sums = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'([0-9a-fA-F]{64})[ \t]+\*?([^\r\n]+)', line)
        require(match, 'Malformed SHA256 declaration')
        digest, name = match.groups()
        require(name not in sums, 'Duplicate checksum filename')
        sums[name] = digest.lower()
    return sums


def crosscheck_release(metadata, manifest, checksums):
    asset = metadata['asset']
    require(manifest.get('schema') == 1, 'Unsupported upstream artifact manifest')
    require(manifest.get('webUiVersion') == metadata['upstream_version'], 'Official manifest version mismatch')
    declared = manifest.get('asset', {})
    for key in ('name', 'size', 'sha256'):
        require(declared.get(key) == asset[key], 'Official manifest disagrees with GitHub asset ' + key)
    require(parse_checksums(checksums).get(asset['name']) == asset['sha256'],
            'Official SHA256 declaration disagrees with GitHub asset digest')


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def make_icons(source, stage):
    from PIL import Image, ImageOps
    from PIL.PngImagePlugin import PngInfo
    from fnos_icon import mask_256
    stage = Path(stage)
    resampling = getattr(Image, 'Resampling', Image).LANCZOS
    with Image.open(source) as original:
        # Use the unrounded upstream artwork, not the Linux-specific silhouette.
        # Retain its previous 7/8 artwork scale; change only the outside contour.
        artwork = ImageOps.contain(original.convert('RGBA'), (224, 224), resampling)
        canvas = Image.new('RGBA', (256, 256), (255, 255, 255, 255))
        canvas.alpha_composite(artwork, ((256 - artwork.width) // 2,
                                         (256 - artwork.height) // 2))
        canvas.putalpha(mask_256())
        pnginfo = PngInfo()
        pnginfo.add(b'sRGB', b'\x00')
        for size in (64, 256):
            icon = canvas if size == 256 else canvas.resize((size, size), resampling)
            icon.save(stage / ('ICON.PNG' if size == 64 else 'ICON_256.PNG'),
                      'PNG', pnginfo=pnginfo)
            path = stage / 'app/ui/images' / ('icon_' + str(size) + '.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            icon.save(path, 'PNG', pnginfo=pnginfo)


def prepare(metadata, stage):
    stage = Path(stage).absolute()
    require(not stage.is_symlink(), 'Stage cannot be a symlink')
    require(not stage.exists() or (stage.is_dir() and not any(stage.iterdir())),
            'Stage must be absent or empty; remove the old staging directory explicitly')
    require((ROOT / 'package/manifest.in').is_file(), 'package/manifest.in is missing')
    stage.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ekko-prepare-', dir=stage.parent) as temporary:
        work = Path(temporary)
        downloads = work / 'downloads'
        downloads.mkdir()
        for key in ASSET_KEYS:
            asset = metadata[key]
            download(asset['url'], downloads / asset['name'], asset['sha256'], asset['size'])
        upstream_manifest = json.loads((downloads / metadata['manifest_asset']['name']).read_text())
        crosscheck_release(metadata, upstream_manifest,
                           (downloads / metadata['checksums_asset']['name']).read_text())
        tree = work / 'stage'
        shutil.copytree(ROOT / 'package', tree, symlinks=True)
        template = (tree / 'manifest.in').read_text()
        require('@VERSION@' in template, 'Manifest version placeholder missing')
        (tree / 'manifest').write_text(template.replace('@VERSION@', metadata['version']))
        (tree / 'manifest.in').unlink()
        unpacked = work / 'webui'
        safe_extract(downloads / metadata['asset']['name'], unpacked, 'webui')
        require(not (tree / 'app/studio').exists(), 'Templates must not bundle an upstream UI')
        shutil.move(str(unpacked / 'webui'), str(tree / 'app/studio'))
        node_name = 'node-v' + metadata['node_version'] + '-linux-x64'
        node_url = 'https://nodejs.org/dist/v' + metadata['node_version'] + '/'
        sums_path = downloads / 'SHASUMS256.txt'
        sums_digest = download(node_url + 'SHASUMS256.txt', sums_path, max_bytes=1024**2)
        node_sums = parse_checksums(sums_path.read_text())
        node_tar = node_name + '.tar.xz'
        require(node_tar in node_sums, 'Official Node SHA256 entry missing')
        download(node_url + node_tar, downloads / node_tar, node_sums[node_tar])
        safe_extract(downloads / node_tar, work / 'node', node_name)
        (tree / 'app/runtime').mkdir(parents=True, exist_ok=True)
        require(not (tree / 'app/runtime/node').exists(), 'Templates must not bundle a Node runtime')
        shutil.move(str(work / 'node' / node_name), str(tree / 'app/runtime/node'))
        raw = 'https://raw.githubusercontent.com/' + REPOSITORY + '/' + metadata['source_commit'] + '/'
        license_path = downloads / 'LICENSE'
        license_digest = download(raw + 'LICENSE', license_path, max_bytes=1024**2)
        shutil.copyfile(license_path, tree / 'LICENSE')
        shutil.copyfile(license_path, tree / 'app/studio/LICENSE')
        icon_path = downloads / 'icon.png'
        icon_digest = download(raw + 'packages/desktop/build/icon.png', icon_path, max_bytes=20 * 1024**2)
        make_icons(icon_path, tree)
        # Only known executable template locations; tar modes on native modules stay intact.
        for directory in ('cmd', 'app/bin'):
            for path in (tree / directory).iterdir():
                if path.is_file() and not path.is_symlink():
                    path.chmod((path.stat().st_mode & 0o777) | 0o111)
        info = dict(metadata)
        info['node'] = {'url': node_url + node_tar, 'sha256': node_sums[node_tar],
                        'shasums_url': node_url + 'SHASUMS256.txt', 'shasums_sha256': sums_digest}
        info['source_files'] = {'LICENSE': license_digest,
                                'packages/desktop/build/icon.png': icon_digest}
        write_json(tree / 'app/build-info.json', info)
        verify_stage(tree, info)
        if stage.exists():
            stage.rmdir()  # previously checked empty; never erase an existing build.
        shutil.move(str(tree), str(stage))
    print('Prepared verified official UI + Node runtime: ' + str(stage))


def repair_fnpack_links(fpk, stage):
    """fnpack 1.2.1 writes link basename instead of readlink() on Linux.

    Repair ONLY links proven to exist in the validated stage. Preserve files,
    modes and official envelope, then update the observed MD5 manifest field.
    No manifest.checksum is synthesized when fnpack does not supply one.
    """
    from verify import archive_name, parse_manifest, validate_members
    stage = Path(stage)
    with tempfile.TemporaryDirectory(prefix='fnpack-links-', dir=stage.parent) as temporary:
        temp = Path(temporary)
        original_app = temp / 'app.original.tgz'
        repaired_app = temp / 'app.tgz'
        with tarfile.open(fpk, 'r:*') as outer:
            entries = validate_members(outer.getmembers())
            require('app.tgz' in entries and 'manifest' in entries, 'Incomplete fnpack output')
            with outer.extractfile(entries['app.tgz']) as src, original_app.open('wb') as dst:
                shutil.copyfileobj(src, dst)
            original_manifest = outer.extractfile(entries['manifest']).read().decode('utf-8')
            require(parse_manifest(original_manifest).get('checksum') == digest_file(original_app, 'md5'),
                    'Official fnpack output checksum mismatch before link repair')
            changed = 0
            with tarfile.open(original_app, 'r:*') as inner, tarfile.open(repaired_app, 'w:gz') as patched:
                members = inner.getmembers()
                includes_app = any(archive_name(m.name) == 'app' and m.isdir() for m in members)
                for member in members:
                    name = archive_name(member.name)
                    relative = name[4:] if includes_app and name.startswith('app/') else name
                    source_path = stage / 'app' / relative
                    if member.issym():
                        require(source_path.is_symlink(), 'Unexpected fnpack symbolic link')
                        target = os.readlink(source_path)
                        if member.linkname != target:
                            member.linkname = target
                            changed += 1
                    patched.addfile(member, inner.extractfile(member) if member.isfile() else None)
            if not changed:
                return
            with tarfile.open(repaired_app) as check:
                validate_members(check.getmembers())
            actual = digest_file(repaired_app, 'md5')
            manifest = outer.extractfile(entries['manifest']).read().decode('utf-8')
            require('checksum' in parse_manifest(manifest), 'fnpack manifest checksum missing')
            manifest = re.sub(r'(?m)^checksum\s*=.*$', 'checksum              = ' + actual, manifest).encode()
            if 'manifest.checksum' in entries:
                old = outer.extractfile(entries['manifest.checksum']).read().decode().strip()
                require(old == digest_file(original_app, 'md5'), 'Unknown manifest.checksum format')
            rebuilt = temp / 'repaired.fpk'
            with tarfile.open(rebuilt, 'w:gz') as patched:
                for name, member in entries.items():
                    if name == 'app.tgz':
                        member.size = repaired_app.stat().st_size
                        with repaired_app.open('rb') as stream:
                            patched.addfile(member, stream)
                    elif name in ('manifest', 'manifest.checksum'):
                        content = manifest if name == 'manifest' else (actual + '\n').encode()
                        member.size = len(content)
                        patched.addfile(member, io.BytesIO(content))
                    else:
                        patched.addfile(member, outer.extractfile(member) if member.isfile() else None)
        shutil.copyfile(rebuilt, fpk)
        print('Restored contained symbolic link targets damaged by fnpack: ' + str(changed))


def package(metadata, stage, output, fnpack):
    stage = Path(stage).absolute()
    output = Path(output).absolute()
    verify_stage(stage, metadata)
    executable = Path(fnpack).absolute()
    require(executable.is_file() and os.access(executable, os.X_OK), 'Official fnpack executable is unavailable')
    require(metadata['fnpack_version'] == '1.2.1', 'Unsupported fnpack version')
    require(digest_file(executable) == '72d2a4095da676b64510b023731a227b369d80f8079bc45ff8a2f802ec0480c1',
            'fnpack binary does not match the pinned official build')
    output.mkdir(parents=True, exist_ok=True)
    intermediate = stage / 'ekko-studio.fpk'
    require(not intermediate.exists(), 'Refusing to overwrite staged fnpack output')
    try:
        subprocess.run([str(executable), 'build'], cwd=str(stage), check=True)
        require(intermediate.is_file(), 'fnpack did not produce ekko-studio.fpk')
        repair_fnpack_links(intermediate, stage)
        verify_fpk(intermediate, metadata)
        filename = 'ekko-studio_v' + metadata['version'] + '_x86_64.fpk'
        final = output / filename
        require(not final.exists() and not Path(str(final) + '.sha256').exists(),
                'Refusing to overwrite existing release artifacts')
        shutil.copyfile(intermediate, final)
        digest = digest_file(final)
        Path(str(final) + '.sha256').write_text(digest + '  ' + filename + '\n')
        shutil.copyfile(stage / 'app/build-info.json', output / 'build-info.json')
        print('Packaged: ' + str(final))
        print('SHA256: ' + digest)
    finally:
        if intermediate.exists():
            intermediate.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare', help='Download, verify and assemble the stage')
    prep.add_argument('--metadata', type=Path, required=True)
    prep.add_argument('--stage', type=Path, required=True)
    pack = commands.add_parser('package', help='Run official fnpack and verify its output')
    pack.add_argument('--metadata', type=Path, required=True)
    pack.add_argument('--stage', type=Path, required=True)
    pack.add_argument('--output', type=Path, required=True)
    pack.add_argument('--fnpack', type=Path, required=True)
    args = parser.parse_args()
    try:
        metadata = read_metadata(args.metadata)
        if args.command == 'prepare':
            prepare(metadata, args.stage)
        else:
            package(metadata, args.stage, args.output, args.fnpack)
    except (VerificationError, OSError, ValueError, KeyError, subprocess.CalledProcessError,
            tarfile.TarError) as error:
        parser.exit(1, 'Build failed: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
