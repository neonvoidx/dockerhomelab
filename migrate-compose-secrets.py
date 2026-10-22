#!/usr/bin/env python3
"""Copy configured .env credentials into private files without logging their values."""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

# Values may be used by other stacks. Creating a file does not configure its consumer.
SECRET_VARIABLES = (
    'CLOUDFLARE_DNS_API_TOKEN', 'CLOUDFLARE_DDNS_API_KEY', 'PLEX_TOKEN',
    'TAILSCALE_AUTH_KEY', 'KOMODO_DB_PASSWORD', 'TRAKT_CLIENT_SECRET',
    'MDB_API_KEY', 'TMDB_API_KEY', 'BW_PASSWD', 'YUBICO_SECRET_KEY',
    'WIREGUARD_PRIVATE_KEY', 'OPENVPN_PASSWORD', 'GLUETUN_PASS',
    'QBT_PASSWORD', 'GSP_GTN_API_KEY',
)
# Only these consumers are wired to file-based secrets in this Compose file.
WIRED_VARIABLES = {'CLOUDFLARE_DNS_API_TOKEN', 'CLOUDFLARE_DDNS_API_KEY',
                   'WIREGUARD_PRIVATE_KEY'}


def check_existing(path):
    """Reject links and non-regular files before reading any existing credential."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise SystemExit('Refusing a symlink or non-regular secret file.')
    if not path.read_bytes().strip():
        raise SystemExit('An existing secret file is empty; no credentials were changed.')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path,
                        default=Path(__file__).resolve().parent / '.env')
    args = parser.parse_args()
    env_file = args.env_file.expanduser().resolve()
    if not env_file.is_file():
        parser.error('The .env file does not exist; run this beside your real deployment .env.')
    root = env_file.parent
    secret_dir = root / 'secrets'
    if secret_dir.is_symlink() or (secret_dir.exists() and not secret_dir.is_dir()):
        raise SystemExit('Refusing a symlinked or non-directory secrets path.')
    helper = {'services': {'extract': {'image': 'scratch', 'environment': {
        key: '${' + key + '-}' for key in SECRET_VARIABLES}}}}
    env = os.environ.copy()
    for key in SECRET_VARIABLES:
        env.pop(key, None)  # Resolve these from .env, not from the calling shell.
    with tempfile.TemporaryDirectory(prefix='compose-secret-extract-') as tmp:
        config = Path(tmp) / 'compose.json'
        config.write_text(json.dumps(helper))
        result = subprocess.run(['docker', 'compose', '--project-directory', str(root),
                                 '--env-file', str(env_file), '-f', str(config),
                                 'config', '--format', 'json'],
                                env=env, capture_output=True, text=True)
        if result.returncode:
            raise SystemExit('Could not resolve credentials from .env. No files written.')
        values = json.loads(result.stdout)['services']['extract']['environment']
        # Compose escapes each dollar sign so rendered config can be reused as input.
        # Decode that serialization once; never interpolate the credential again.
        values = {key: value.replace('$$', '$') if isinstance(value, str) else value
                  for key, value in values.items()}
    plans = []
    # Preflight all names before changing any existing credentials or permissions.
    for key in SECRET_VARIABLES:
        path = secret_dir / key.lower()
        exists = check_existing(path)
        value = values.get(key)
        present = isinstance(value, str) and bool(value.strip())
        if present and exists and path.read_bytes() != value.encode():
            raise SystemExit('An existing secret differs from .env; no credentials were changed.')
        if key in WIRED_VARIABLES and not (present or exists):
            raise SystemExit('A required wired credential is missing from both .env and secrets/: '
                             + key + '. No files written.')
        plans.append((key, path, value if present else None, exists))
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    secret_dir.chmod(0o700)
    prepared = []
    skipped = []
    for key, path, value, exists in plans:
        if value is None and not exists:
            skipped.append(key)
            continue
        if not exists:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(value.encode())
        path.chmod(0o600)
        prepared.append(key)
    ignore = secret_dir / '.gitignore'
    if not ignore.exists():
        with ignore.open('x') as stream:
            stream.write('*\n!.gitignore\n')
    print('Prepared or reused ' + str(len(prepared)) + ' secret files '
          '(directory 0700, files 0600). No values printed.')
    if skipped:
        print('Not present; skipped: ' + ', '.join(skipped))
    print('The original .env is unchanged.')
    print('After successful deployment, these entries may be removed from .env: '
          + ', '.join(sorted(WIRED_VARIABLES)))
    print('Keep all other entries until every consuming application or script reads its secret file.')


if __name__ == '__main__':
    main()
