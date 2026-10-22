import os
from pathlib import Path
import runpy
import shutil
import stat
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'migrate-compose-secrets.py'
SPEC = runpy.run_path(str(SCRIPT))
VARIABLES = SPEC['SECRET_VARIABLES']
WIRED = SPEC['WIRED_VARIABLES']


@unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI required')
class SecretMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = self.root / '.env'

    def write_env(self, keys):
        values = {key: 'synthetic-' + key + '-=$ with spaces' for key in keys}
        self.env.write_text(''.join(key + "='" + value + "'\n" for key, value in values.items()))
        return values

    def run_script(self, **extra_env):
        return subprocess.run(['python3', str(SCRIPT), '--env-file', str(self.env)],
                              env=dict(os.environ, **extra_env), capture_output=True, text=True)

    def test_all_names_quoting_permissions_and_no_value_output(self):
        values = self.write_env(VARIABLES)
        original = self.env.read_bytes()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for key, value in values.items():
            path = self.root / 'secrets' / key.lower()
            self.assertEqual(path.read_text(), value)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE((self.root / 'secrets').stat().st_mode), 0o700)
        self.assertEqual(self.env.read_bytes(), original)
        self.assertIn('Prepared or reused 15 secret files', result.stdout)
        self.assertFalse((self.root / 'secrets/tailscal_auth_key').exists())
        self.assertFalse((self.root / 'secrets/opencpn_password').exists())
        self.assertEqual(self.run_script().returncode, 0)

    def test_literal_dollar_sequences_are_preserved(self):
        self.write_env(WIRED)
        value = 'synthetic-$word-$$literal-${REFERENCE}-$$$$'
        with self.env.open('a') as stream:
            stream.write("QBT_PASSWORD='" + value + "'\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'secrets/qbt_password').read_text(), value)

    def test_optional_names_are_skipped_without_shell_fallback(self):
        self.write_env(WIRED)
        result = self.run_script(PLEX_TOKEN='synthetic-shell-value')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'secrets/plex_token').exists())
        self.assertIn('Not present; skipped:', result.stdout)

    def test_rerun_after_removing_wired_values_from_env(self):
        values = self.write_env(WIRED)
        self.assertEqual(self.run_script().returncode, 0)
        self.env.write_text('TZ=America/New_York\n')
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for key, value in values.items():
            self.assertEqual((self.root / 'secrets' / key.lower()).read_text(), value)

    def test_existing_two_secret_migration_can_add_ddns(self):
        secret_dir = self.root / 'secrets'; secret_dir.mkdir()
        for key in ('CLOUDFLARE_DNS_API_TOKEN', 'WIREGUARD_PRIVATE_KEY'):
            (secret_dir / key.lower()).write_text('synthetic-existing-' + key)
        self.write_env(['CLOUDFLARE_DDNS_API_KEY', 'PLEX_TOKEN'])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((secret_dir / 'plex_token').exists())
        self.assertTrue((secret_dir / 'cloudflare_ddns_api_key').exists())

    def test_missing_wired_key_writes_no_secret(self):
        self.write_env(['CLOUDFLARE_DNS_API_TOKEN'])
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'secrets').exists())

    def test_conflict_preflight_does_not_write_other_files(self):
        self.write_env(VARIABLES)
        secret_dir = self.root / 'secrets'; secret_dir.mkdir()
        token = secret_dir / 'cloudflare_dns_api_token'; token.write_text('different-value')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(token.read_text(), 'different-value')
        self.assertEqual([p.name for p in secret_dir.iterdir()], ['cloudflare_dns_api_token'])

    def test_empty_existing_secret_is_rejected(self):
        self.write_env(WIRED)
        secret_dir = self.root / 'secrets'; secret_dir.mkdir()
        (secret_dir / 'wireguard_private_key').touch()
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertFalse((secret_dir / 'cloudflare_dns_api_token').exists())

    def test_symlinked_secret_is_rejected(self):
        self.write_env(WIRED)
        secret_dir = self.root / 'secrets'; secret_dir.mkdir()
        target = self.root / 'target'; target.write_text('preserve-this')
        (secret_dir / 'wireguard_private_key').symlink_to(target)
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual(target.read_text(), 'preserve-this')
        self.assertFalse((secret_dir / 'cloudflare_dns_api_token').exists())


if __name__ == '__main__':
    unittest.main()
