import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts'/'migrate-compose-secrets.py'

@unittest.skipUnless(shutil.which('docker'),'Docker Compose CLI required')
class SecretMigrationTests(unittest.TestCase):
    def test_round_trip_permissions_and_overwrite_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=root/'.env'
            original='CLOUDFLARE_DNS_API_TOKEN="synthetic-token"\nWIREGUARD_PRIVATE_KEY=\'synthetic-key=\'\n'
            env.write_text(original)
            def run():return subprocess.run(['python3',str(SCRIPT),'--env-file',str(env)],capture_output=True,text=True)
            result=run();self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn('synthetic-token',result.stdout+result.stderr)
            self.assertNotIn('synthetic-key',result.stdout+result.stderr)
            self.assertEqual(env.read_text(),original)
            token=root/'secrets/cloudflare_dns_api_token';key=root/'secrets/wireguard_private_key'
            self.assertEqual(token.read_text(),'synthetic-token');self.assertEqual(key.read_text(),'synthetic-key=')
            self.assertEqual(stat.S_IMODE(token.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(token.parent.stat().st_mode),0o700)
            self.assertEqual(run().returncode,0)
            token.write_text('different-value')
            self.assertNotEqual(run().returncode,0);self.assertEqual(token.read_text(),'different-value')

    def test_missing_key_writes_no_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=root/'.env';env.write_text('CLOUDFLARE_DNS_API_TOKEN=synthetic\n')
            result=subprocess.run(['python3',str(SCRIPT),'--env-file',str(env)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertFalse((root/'secrets').exists())

if __name__=='__main__':unittest.main()
