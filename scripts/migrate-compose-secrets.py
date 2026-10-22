#!/usr/bin/env python3
"""Copy two .env credentials into private Compose secret files without logging them."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path(__file__).resolve().parent.parent / '.env')
    args = parser.parse_args()
    env_file = args.env_file.expanduser().resolve()
    if not env_file.is_file():
        parser.error('The .env file does not exist; run this beside your real deployment .env.')
    root = env_file.parent
    keys = {'cloudflare_dns_api_token': 'CLOUDFLARE_DNS_API_TOKEN',
            'wireguard_private_key': 'WIREGUARD_PRIVATE_KEY'}
    helper = {'services': {'extract': {'image': 'scratch', 'environment': {
        key: '${' + key + ':?required}' for key in keys.values()}}}}
    env = os.environ.copy()
    for key in keys.values():
        env.pop(key, None)  # Read these values from the supplied .env, not the shell.
    with tempfile.TemporaryDirectory(prefix='compose-secret-extract-') as tmp:
        config = Path(tmp) / 'compose.json'
        config.write_text(json.dumps(helper))
        result = subprocess.run(['docker', 'compose', '--project-directory', str(root),
                                 '--env-file', str(env_file), '-f', str(config),
                                 'config', '--format', 'json'],
                                env=env, capture_output=True, text=True)
        if result.returncode:
            raise SystemExit('Could not resolve both required credentials from .env. No files written.')
        values = json.loads(result.stdout)['services']['extract']['environment']
    if any(not isinstance(values.get(key), str) or not values[key].strip() for key in keys.values()):
        raise SystemExit('A required credential is empty. No files written.')
    secret_dir = root / 'secrets'
    if secret_dir.is_symlink():
        raise SystemExit('Refusing a symlinked secrets directory.')
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    secret_dir.chmod(0o700)
    # Preflight every target before writing; never overwrite a different secret.
    for name, key in keys.items():
        path = secret_dir / name
        if path.is_symlink():
            raise SystemExit('Refusing a symlinked secret file.')
        if path.exists() and (not path.is_file() or path.read_bytes() != values[key].encode()):
            raise SystemExit('An existing secret differs; no existing files were overwritten.')
    for name, key in keys.items():
        path = secret_dir / name
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(values[key].encode())
        path.chmod(0o600)
    ignore = secret_dir / '.gitignore'
    if not ignore.exists():
        with ignore.open('x') as stream:
            stream.write('*\n!.gitignore\n')
    print('Prepared two secret files (directory 0700, files 0600). No values printed.')
    print('The original .env is unchanged. Remove those two entries after deployment succeeds.')


if __name__ == '__main__':
    main()
