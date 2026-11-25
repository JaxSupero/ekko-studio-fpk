#!/usr/bin/env python3
"""Run on the Linux x86_64 builder; this is not a real fnOS install test."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import urllib.request

NATIVE_TEST = r'''
const { createRequire } = require('node:module');
const { resolve } = require('node:path');
const requireApp = createRequire(resolve(process.argv[1], 'package.json'));
if (process.platform !== 'linux' || process.arch !== 'x64') throw Error('Expected Linux x64 runtime');
const { DatabaseSync } = require('node:sqlite');
const db = new DatabaseSync(':memory:');
if (db.prepare('select 42 as n').get().n !== 42) throw Error('SQLite failed');
db.close();
const sharp = requireApp('sharp');
const pty = requireApp('node-pty');
(async () => {
  await sharp({create: {width: 2, height: 2, channels: 3, background: '#ffffff'}}).png().toBuffer();
  await new Promise((ok, fail) => {
    const terminal = pty.spawn('/bin/sh', ['-c', 'printf EKKo_PTY_OK'], {cols: 80, rows: 24, env: process.env});
    let output = '';
    const timeout = setTimeout(() => {terminal.kill(); fail(Error('PTY timeout'));}, 10000);
    terminal.onData(data => { output += data; });
    terminal.onExit(event => {clearTimeout(timeout); event.exitCode === 0 && output.includes('EKKo_PTY_OK') ? ok() : fail(Error('PTY failed'));});
  });
  console.log('SQLite, sharp, and node-pty passed');
})().catch(error => {console.error(error); process.exit(1);});
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True, type=Path)
    args = parser.parse_args()
    stage = args.stage.resolve()
    node = stage / 'app/runtime/node/bin/node'
    studio = stage / 'app/studio'
    subprocess.run([str(node), '-e', NATIVE_TEST, str(studio)], check=True, timeout=45)
    subprocess.run([str(node), '--check', str(stage / 'app/bin/service.mjs')], check=True)
    for script in (stage / 'cmd').iterdir():
        if script.is_file():
            subprocess.run(['bash', '-n', str(script)], check=True)
    with tempfile.TemporaryDirectory(prefix='ekko-fpk-smoke-') as temporary:
        root = Path(temporary)
        env = {**os.environ, 'TRIM_APPDEST': str(stage / 'app'),
               'TRIM_PKGHOME': str(root / 'home'), 'TRIM_PKGVAR': str(root / 'var'),
               'TRIM_TEMP_LOGFILE': str(root / 'install-errors.log'),
               'TRIM_USERNAME': os.environ.get('USER', 'runner')}
        for key in ('AUTH_TOKEN', 'HERMES_BIN', 'HERMES_AGENT_ROOT', 'HERMES_HOME',
                    'HERMES_WEB_UI_HOME', 'HERMES_DESKTOP_RUNTIME_DIR'):
            env.pop(key, None)
        def run(script, *extra, check=True, timeout=100):
            return subprocess.run(['bash', str(stage / 'cmd' / script), *extra], env=env,
                                  check=check, timeout=timeout)
        run('install_callback')
        marker = root / 'home' / 'preserve-user-data.txt'
        marker.write_text('must survive upgrade and restart')
        try:
            run('main', 'start')
            run('main', 'status')
            with urllib.request.urlopen('http://127.0.0.1:8648/health', timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError('Health endpoint did not return 200')
            run('main', 'stop')
            run('main', 'stop')
            if run('main', 'status', check=False).returncode != 3:
                raise RuntimeError('Stopped fnOS status must be exit 3')
            run('upgrade_callback')
            if not marker.exists():
                raise RuntimeError('Upgrade removed persistent user data')
            with socket.socket() as occupied:
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind(('127.0.0.1', 8648))
                occupied.listen(1)
                if run('main', 'start', check=False, timeout=100).returncode == 0:
                    raise RuntimeError('Service must reject a port owned by another application')
                occupied.getsockname()  # Port conflict handling must not close/kill the owner.
            print('Service health, safe port conflicts, idempotent stop, and data preservation passed')
        finally:
            run('main', 'stop', check=False, timeout=45)
            for log in (root / 'var').glob('*.log') if (root / 'var').exists() else []:
                print('Diagnostic log retained during smoke test:', log.name)


if __name__ == '__main__':
    main()
