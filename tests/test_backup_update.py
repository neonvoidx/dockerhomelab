"""Exercise orchestration and recovery without Docker, NFS, or real rsync writes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'backup-update.sh'
MOCK = '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
mode=os.environ.get('TEST_MODE','success')
with open(os.environ['CALL_LOG'],'a') as f:f.write(name+' '+' '.join(args)+'\\n')
if name=='docker':
 if args[0]=='inspect': print('plex sha256:'+'a'*64)
 elif 'config' in args and '--format' in args: print('{"secrets":{"missing":{"file":"/nonexistent-homelab-test-secret"}}}' if mode=='missing_secret' else '{"secrets":{}}')
 elif 'ps' in args: print('fixture-container')
 elif 'pull' in args and mode=='pull_failure':sys.exit(1)
 elif 'down' in args and mode=='down_failure':sys.exit(1)
 elif 'up' in args and mode=='startup_failure' and not any('previous-images.json' in a for a in args):sys.exit(1)
elif name=='findmnt':
 if mode=='missing_mount':sys.exit(1)
 if mode=='mount_lost_after_down' and ' down ' in pathlib.Path(os.environ['CALL_LOG']).read_text():sys.exit(1)
 print(('ext4' if mode=='local_mount' else 'nfs4') if args[-1]=='FSTYPE' else 'synology:/volume1/docker')
elif name=='rsync':
 if mode=='backup_failure':sys.exit(23)
 if mode=='project_backup_failure' and 'dockerhomelab/' in args[-1]:sys.exit(23)
elif name=='flock':
 if mode=='locked':sys.exit(1)
elif name=='realpath':print(os.path.realpath(args[-1]))
elif name=='timeout':sys.exit(subprocess.run(args[1:]).returncode)
'''

class BackupUpdateTests(unittest.TestCase):
    def run_case(self, mode='success', **extra):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); bins=root/'bin'; bins.mkdir()
            for name in ('docker','findmnt','rsync','flock','realpath','timeout'):
                p=bins/name; p.write_text(MOCK); p.chmod(0o755)
            project=root/'project';project.mkdir();(project/'docker-compose.yaml').write_text('services: {}\n')
            source=root/'source';source.mkdir();(source/'database').write_text('fixture')
            mount=root/'synology';mount.mkdir(); log=root/'calls'
            env=dict(os.environ,PATH=str(bins)+os.pathsep+os.environ['PATH'],
                     STACK_DIR=str(project),CONFIG_SOURCE=str(source),SYNOLOGY_MOUNT=str(mount),
                     BACKUP_ROOT=str(mount/'backups'),TEST_MODE=mode,CALL_LOG=str(log),**extra)
            result=subprocess.run(['bash',str(SCRIPT)],env=env,capture_output=True,text=True)
            calls=log.read_text().splitlines() if log.exists() else []
            return result,calls

    def test_success_orders_pull_backup_and_restart(self):
        result,calls=self.run_case();self.assertEqual(result.returncode,0,result.stderr)
        pull=next(i for i,c in enumerate(calls) if c.endswith(' pull'))
        down=next(i for i,c in enumerate(calls) if ' down ' in c)
        sync=[i for i,c in enumerate(calls) if c.startswith('rsync ')]
        up=next(i for i,c in enumerate(calls) if ' up ' in c)
        self.assertLess(pull,down);self.assertLess(down,sync[0]);self.assertLess(sync[-1],up)
        self.assertEqual(len(sync),2)
        self.assertTrue(all('--delete' in calls[i] and '--exclude=plex/' in calls[i] for i in sync))
        self.assertIn('--wait',calls[up]);self.assertIn('--pull never',calls[up])

    def test_mount_guards_prevent_shutdown(self):
        for mode in ('missing_mount','local_mount'):
            with self.subTest(mode=mode):
                result,calls=self.run_case(mode);self.assertNotEqual(result.returncode,0)
                self.assertFalse(any(' down ' in c or c.endswith(' pull') for c in calls))

    def test_expected_nfs_source(self):
        result,calls=self.run_case(EXPECTED_SYNOLOGY_SOURCE='wrong:/share')
        self.assertNotEqual(result.returncode,0);self.assertFalse(any(' down ' in c for c in calls))

    def test_pull_failure_keeps_stack_running(self):
        result,calls=self.run_case('pull_failure');self.assertNotEqual(result.returncode,0)
        self.assertFalse(any(' down ' in c or ' up ' in c for c in calls))

    def test_backup_and_shutdown_failures_recover_old_images(self):
        for mode in ('backup_failure','project_backup_failure','down_failure','mount_lost_after_down'):
            with self.subTest(mode=mode):
                result,calls=self.run_case(mode);self.assertNotEqual(result.returncode,0)
                ups=[c for c in calls if ' up ' in c]
                self.assertEqual(len(ups),1)
                self.assertIn('previous-images.json',ups[0]);self.assertIn('--pull never plex',ups[0])

    def test_failed_upgrade_does_not_automatically_rollback_databases(self):
        result,calls=self.run_case('startup_failure');self.assertNotEqual(result.returncode,0)
        ups=[c for c in calls if ' up ' in c];self.assertEqual(len(ups),1)
        self.assertNotIn('previous-images.json',ups[0])

    def test_missing_secret_prevents_shutdown(self):
        result,calls=self.run_case('missing_secret');self.assertNotEqual(result.returncode,0)
        self.assertFalse(any(' down ' in c or c.endswith(' pull') for c in calls))

    def test_lock_prevents_concurrent_runs(self):
        result,calls=self.run_case('locked');self.assertNotEqual(result.returncode,0)
        self.assertFalse(any(c.startswith('docker ') for c in calls))

if __name__=='__main__':unittest.main()
