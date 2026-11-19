"""Backup job failure regressions; subprocesses simulate Docker and age only."""
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


SCRIPTS = Path(__file__).resolve().parents[1]
FAKE = r'''
import json, os, pathlib, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ['JOB_TEST_ROOT'])
def log(event):
    with (root/'calls').open('a') as output: output.write(event+'\n')
if tool == 'age':
    data = sys.stdin.buffer.read()
    if os.environ.get('INVALID_RECIPIENT'): raise SystemExit(1)
    encrypted = b'age-encryption.org/v1\n'+data
    if '-o' in args: pathlib.Path(args[args.index('-o')+1]).write_bytes(encrypted)
    else: sys.stdout.buffer.write(encrypted)
elif tool == 'docker':
    if 'backup-transfer' in args:
        operation = args[args.index('backup-transfer')+1]
        log(operation)
        if operation == 'preflight' and os.environ.get('REPLACE_RUNTIME_DURING_PREFLIGHT'):
            pathlib.Path(os.environ['ENV_FILE']).write_text('POSTGRES_DB=other_snapshot\nPOSTGRES_USER=workflow_owner\n')
        if operation == 'preflight' and os.environ.get('REPLACE_MONITOR_DURING_PREFLIGHT'):
            monitor = pathlib.Path(os.environ['BACKUP_NOTIFICATION_ENV_DIR']) / 'processing-monitor.env'
            monitor.unlink()
            kind = os.environ['REPLACE_MONITOR_DURING_PREFLIGHT']
            if kind == 'symlink':
                other = root / 'unrelated-fixture'
                other.write_bytes(b'unrelated fixture must not be archived')
                monitor.symlink_to(other)
            else:
                monitor.write_bytes(b'x' * 65537)
                monitor.chmod(0o600)
        if os.environ.get('FAIL_TRANSFER') == operation: raise SystemExit(1)
        print(json.dumps({'status':'uploaded' if operation=='upload' else 'configuration_valid',
                          'remote_content_verified':True,'completion_keys':['map1/daily/fixture/COMPLETE.json']}))
    else:
        container = dict(os.environ, POSTGRES_DB='workflow_fixture', POSTGRES_USER='workflow_owner')
        os.execve('/bin/sh', args[args.index('sh'):], container)
elif tool == 'pg_dump':
    log('dump')
    (root/'dump-target.json').write_text(json.dumps({'database':args[args.index('-d')+1], 'user':args[args.index('-U')+1]}))
    if os.environ.get('REPLACE_SOURCE_DURING_DUMP'):
        pathlib.Path(os.environ['BACKUP_SOURCE_ARCHIVE']).write_bytes(b'replacement deployment source')
    if os.environ.get('REPLACE_VPN_DURING_DUMP'):
        (root/'vpn-live/etc/map1-ogm-vpn/client.ovpn').write_bytes(b'later live profile')
    sys.stdout.buffer.write(b'PGDMP\x00isolated workflow fixture')
    if os.environ.get('FAIL_DUMP'): raise SystemExit(1)
else: raise SystemExit(2)
'''


class OffsiteJobTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.offsite-job-', dir=SCRIPTS/'tests')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        tools = self.root/'bin'
        tools.mkdir()
        fake = tools/'fake'
        fake.write_text(f'#!{sys.executable}\n'+FAKE)
        fake.chmod(0o700)
        for name in ('docker','pg_dump','age'):
            (tools/name).symlink_to(fake)
        self.config = self.root/'runtime.env'
        self.config.write_text('POSTGRES_DB=workflow_fixture\nPOSTGRES_USER=workflow_owner\n')
        self.s3 = self.root/'storage.env'
        self.s3.write_text('BACKUP_S3_BUCKET=workflow_fixture\nBACKUP_S3_ENDPOINT_URL=https://s3.project.invalid\nBACKUP_S3_REGION=eu-central\n')
        self.source = self.root/'source.tar.gz'
        with tarfile.open(self.source,'w:gz') as archive:
            info = tarfile.TarInfo('app.py'); info.size = 7
            archive.addfile(info,io.BytesIO(b'pass\n\n\n'))
        self.backups = self.root/'backups'
        self.env = dict(PATH=str(tools)+':/usr/bin:/bin',JOB_TEST_ROOT=str(self.root),
                        ENV_FILE=str(self.config),BACKUP_S3_ENV_FILE=str(self.s3),
                        BACKUP_SOURCE_ARCHIVE=str(self.source),BACKUP_AGE_RECIPIENT='test-recipient',
                        BACKUP_DIR=str(self.backups),BACKUP_MIN_FREE_GB='0',BACKUP_MAX_DUMP_GB='1')

    def run_job(self,**changes):
        return subprocess.run(['bash',str(SCRIPTS/'backup-offsite.sh')],env=dict(self.env,**changes),
                              cwd=SCRIPTS.parent,text=True,capture_output=True,timeout=20)

    def prior(self,name,verified):
        path = self.backups/name
        path.mkdir(parents=True)
        (path/'retained.age').write_bytes(b'older recovery copy')
        if verified: (path/'.offsite-verified.json').write_text('{}')
        return path

    def test_failed_transfer_retains_new_and_previous_bundles(self):
        previous = self.prior('bundle-20261001T020000Z-AbCd1234',True)
        result = self.run_job(FAIL_TRANSFER='upload')
        self.assertNotEqual(result.returncode,0)
        self.assertTrue(previous.exists())
        new = [p for p in self.backups.glob('bundle-*') if p != previous]
        self.assertEqual(len(new),1)
        self.assertTrue((new[0]/'configuration.tar.age').is_file())
        self.assertEqual(len(list(new[0].glob('*.dump.age'))),1)
        self.assertFalse((new[0]/'.offsite-verified.json').exists())
        self.assertFalse(list(self.backups.glob('.bundle-*')))

    def test_success_retries_pending_before_dump_and_prunes_only_completed_bundles(self):
        previous = self.prior('bundle-20261001T020000Z-AbCd1234',True)
        pending = self.prior('bundle-20261002T020000Z-EfGh5678',False)
        unrelated = self.prior('bundle-keep-client-files',True)
        result = self.run_job()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(previous.exists())
        self.assertFalse(pending.exists())
        self.assertTrue(unrelated.exists())
        completed = [p for p in self.backups.glob('bundle-*') if p not in (pending,unrelated)]
        self.assertEqual(len(completed),1)
        self.assertTrue((completed[0]/'.offsite-verified.json').is_file())
        self.assertFalse((completed[0]/'configuration').exists())
        self.assertEqual((self.root/'calls').read_text().splitlines(),['preflight','upload','dump','upload'])

    def test_failed_retry_does_not_generate_another_dump(self):
        pending = self.prior('bundle-20261002T020000Z-EfGh5678',False)
        result = self.run_job(FAIL_TRANSFER='upload')
        self.assertNotEqual(result.returncode,0)
        self.assertTrue(pending.exists())
        self.assertEqual((self.root/'calls').read_text().splitlines(),['preflight','upload'])
        self.assertEqual(list(self.backups.glob('bundle-*')),[pending])

    def test_retry_of_older_bundle_cannot_delete_newer_copy_when_new_dump_fails(self):
        newer = self.prior('bundle-20261023T020000Z-AbCd1234',True)
        older = self.prior('bundle-20261002T020000Z-EfGh5678',False)
        result = self.run_job(FAIL_DUMP='1')
        self.assertNotEqual(result.returncode,0)
        self.assertTrue(newer.exists())
        self.assertTrue(older.exists())
        self.assertEqual((self.root/'calls').read_text().splitlines(),['preflight','upload','dump'])

    def test_missing_storage_file_does_not_start_dump(self):
        self.s3.unlink()
        result = self.run_job()
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'calls').exists())

    def test_failed_preflight_does_not_start_dump(self):
        result = self.run_job(FAIL_TRANSFER='preflight')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((self.root/'calls').read_text(),'preflight\n')
        self.assertFalse(list(self.backups.glob('bundle-*')))

    def test_invalid_recipient_and_space_floor_do_not_start_dump(self):
        for changes in ({'INVALID_RECIPIENT':'1'},{'BACKUP_MIN_FREE_GB':'99999'}):
            result = self.run_job(**changes)
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn('dump', (self.root/'calls').read_text() if (self.root/'calls').exists() else '')

    def test_multiple_reserve_paths_are_optional_and_preserve_spaces(self):
        extra = self.root / 'separate database'
        extra.mkdir()
        result = self.run_job(BACKUP_RESERVE_PATHS=str(extra) + '\n' + str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root/'calls').read_text().splitlines(), ['preflight', 'dump', 'upload'])

    def test_invalid_explicit_reserve_path_fails_before_transfer_or_dump(self):
        for path in ('relative/path', str(self.root / 'missing')):
            result = self.run_job(BACKUP_RESERVE_PATHS=path)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root/'calls').exists())
            self.assertNotIn(path, result.stderr)

    def test_extra_filesystem_breach_after_dump_preserves_verified_copy_and_skips_upload(self):
        extra = self.root / 'pgdata'
        extra.mkdir()
        previous = self.prior('bundle-20261001T020000Z-AbCd1234', True)
        shim = self.root / 'bin/python3'
        shim.write_text(f'#!{sys.executable}\n' + r'''
import os,pathlib,runpy,sys
if pathlib.Path(sys.argv[1]).name != 'backup_budget.py':
 os.execv(sys.executable,[sys.executable,*sys.argv[1:]])
actual=os.statvfs
root=pathlib.Path(os.environ['JOB_TEST_ROOT'])
def sample(path):
 values=list(actual(path))
 if pathlib.Path(path)==root/'pgdata' and (root/'dump-target.json').exists():values[7]=0
 return os.statvfs_result(values)
os.statvfs=sample
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
''')
        shim.chmod(0o700)
        result = self.run_job(BACKUP_RESERVE_PATHS=str(extra))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('inode reserve breached', result.stderr)
        self.assertEqual((self.root/'calls').read_text().splitlines(), ['preflight', 'dump'])
        self.assertEqual(list(self.backups.glob('bundle-*')), [previous])
        self.assertFalse(list(self.backups.glob('.bundle-*')))


    def test_recovery_configuration_keeps_effective_deployment_and_backup_settings(self):
        manifest=self.root/'source-manifest.json'
        manifest.write_text(json.dumps(self.manifest_value()))
        override=self.root/'deploy.override.json'
        override.write_text('{"services":{"caddy":{"environment":{"CADDY_DEMO_DOMAIN":"localhost"}}}}')
        job=self.root/'backup-job.env'
        job.write_text('BACKUP_MIN_FREE_GB=40\n')
        result=self.run_job(BACKUP_SOURCE_MANIFEST=str(manifest),BACKUP_DEPLOY_OVERRIDE_FILE=str(override),BACKUP_JOB_ENV_FILE=str(job))
        self.assertEqual(result.returncode,0,result.stderr)
        encrypted=next(self.backups.glob('bundle-*/configuration.tar.age')).read_bytes()
        with tarfile.open(fileobj=io.BytesIO(encrypted.split(b'\n',1)[1]),mode='r:gz') as archive:
            for name,expected in [('runtime.env',self.config),('backup-s3.env',self.s3),('source.tar.gz',self.source),
                                  ('source-manifest.json',manifest),('deploy.override.json',override),('backup-job.env',job)]:
                self.assertEqual(archive.extractfile('./'+name).read(),expected.read_bytes())

    def test_release_archive_mismatch_does_not_start_dump_or_transfer(self):
        manifest=self.root/'source-manifest.json'
        manifest.write_text(json.dumps({'source_sha256':'0'*64}))
        result=self.run_job(BACKUP_SOURCE_MANIFEST=str(manifest))
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'calls').exists())

    def test_missing_effective_deployment_override_stops_before_dump(self):
        result=self.run_job(BACKUP_DEPLOY_OVERRIDE_FILE=str(self.root/'missing.override.json'))
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'calls').exists())

    def test_deployment_replacement_during_dump_cannot_change_frozen_recovery_source(self):
        original=self.source.read_bytes()
        manifest=self.root/'source-manifest.json'
        manifest.write_text(json.dumps(self.manifest_value()))
        result=self.run_job(BACKUP_SOURCE_MANIFEST=str(manifest),REPLACE_SOURCE_DURING_DUMP='1')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotEqual(self.source.read_bytes(),original)
        encrypted=next(self.backups.glob('bundle-*/configuration.tar.age')).read_bytes()
        with tarfile.open(fileobj=io.BytesIO(encrypted.split(b'\n',1)[1]),mode='r:gz') as archive:
            stored=archive.extractfile('./source.tar.gz').read()
            expected=json.load(archive.extractfile('./source-manifest.json'))['source_sha256']
            self.assertEqual(stored,original)
            self.assertEqual(hashlib.sha256(stored).hexdigest(),expected)

    def test_notification_recovery_copies_only_two_scoped_ping_environments(self):
        directory=self.root/'notifications';directory.mkdir()
        expected={}
        for unit in ['map1-offsite-backup.service','map1-offsite-backup-check.service']:
            name='backup-alert-'+unit+'.env'
            content='BACKUP_ALERT_EMAIL_TO=\nBACKUP_ALERT_WEBHOOK=https://hc-ping.com/00000000-0000-4000-8000-000000000001/fail\n'
            (directory/name).write_text(content);expected[name]=content.encode()
        (directory/'owner-api-key.private').write_text('owner key must remain off host and outside backup configuration')
        result=self.run_job(BACKUP_NOTIFICATION_ENV_DIR=str(directory))
        self.assertEqual(result.returncode,0,result.stderr)
        encrypted=next(self.backups.glob('bundle-*/configuration.tar.age')).read_bytes()
        with tarfile.open(fileobj=io.BytesIO(encrypted.split(b'\n',1)[1]),mode='r:gz') as archive:
            for name,content in expected.items():self.assertEqual(archive.extractfile('./'+name).read(),content)
            self.assertNotIn('./owner-api-key.private',archive.getnames())

    def test_incomplete_notification_recovery_configuration_stops_before_dump(self):
        directory=self.root/'notifications';directory.mkdir()
        (directory/'backup-alert-map1-offsite-backup.service.env').write_text('BACKUP_ALERT_EMAIL_TO=\n')
        result=self.run_job(BACKUP_NOTIFICATION_ENV_DIR=str(directory))
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'calls').exists())

    def monitor_notifications(self):
        directory = self.root / 'notifications'
        directory.mkdir()
        for unit in ['map1-offsite-backup.service', 'map1-offsite-backup-check.service']:
            (directory / ('backup-alert-' + unit + '.env')).write_text('BACKUP_ALERT_EMAIL_TO=\n')
        monitor = directory / 'processing-monitor.env'
        monitor.write_text('MAP1_PROCESSING_HEARTBEAT_URL=https://hc-ping.com/00000000-0000-4000-8000-000000000002\n')
        monitor.chmod(0o600)
        return directory, monitor

    def test_current_processing_notification_is_recoverable_only_inside_encrypted_configuration(self):
        directory, monitor = self.monitor_notifications()
        content = monitor.read_bytes()
        result = self.run_job(BACKUP_NOTIFICATION_ENV_DIR=str(directory))
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = next(self.backups.glob('bundle-*'))
        encrypted = (bundle / 'configuration.tar.age').read_bytes()
        with tarfile.open(fileobj=io.BytesIO(encrypted.split(b'\n', 1)[1]), mode='r:gz') as archive:
            self.assertEqual(archive.extractfile('./processing-monitor.env').read(), content)
            self.assertEqual(archive.getmember('./processing-monitor.env').mode & 0o077, 0)
        for path in bundle.iterdir():
            if path.name != 'configuration.tar.age':
                self.assertNotIn(content, path.read_bytes())
        self.assertNotIn(content.decode().strip(), result.stdout + result.stderr)

    def test_unsafe_processing_notification_stops_before_dump(self):
        directory, monitor = self.monitor_notifications()
        for kind in ['public', 'readonly', 'executable', 'empty', 'oversized', 'symlink']:
            with self.subTest(kind=kind):
                monitor.unlink()
                monitor.write_text('MAP1_PROCESSING_HEARTBEAT_URL=fixture\n')
                monitor.chmod(0o600)
                if kind == 'public':
                    monitor.chmod(0o644)
                elif kind == 'readonly':
                    monitor.chmod(0o400)
                elif kind == 'executable':
                    monitor.chmod(0o700)
                elif kind == 'empty':
                    monitor.write_bytes(b'')
                elif kind == 'oversized':
                    monitor.write_bytes(b'x' * 65537)
                else:
                    monitor.unlink()
                    monitor.symlink_to(directory / 'absent')
                result = self.run_job(BACKUP_NOTIFICATION_ENV_DIR=str(directory))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / 'calls').exists())

    def test_processing_notification_replacement_during_preflight_is_not_archived(self):
        directory, monitor = self.monitor_notifications()
        for kind in ['symlink', 'oversized']:
            with self.subTest(kind=kind):
                monitor.unlink()
                monitor.write_text('MAP1_PROCESSING_HEARTBEAT_URL=fixture\n')
                monitor.chmod(0o600)
                calls = self.root / 'calls'
                if calls.exists():
                    calls.unlink()
                result = self.run_job(BACKUP_NOTIFICATION_ENV_DIR=str(directory),
                                      REPLACE_MONITOR_DURING_PREFLIGHT=kind)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('dump', calls.read_text())
                self.assertFalse(list(self.backups.glob('bundle-*')))
                self.assertFalse(list(self.backups.glob('.bundle-*')))

    def manifest_value(self):
        # New sidecars require the native complete recovery manifest, not a
        # made-up same-snapshot run ID or a minimal digest-only legacy manifest.
        scope = ('ACTIVE_REGION_CODE=TR-CURRENT-FOREST-20261114\n'
                 'ACTIVE_GRID_VERSION=stage3-current-forest-20261114\n'
                 'STAGE4_REGION_CODE=TR-CURRENT-FOREST-20261114\n'
                 'STAGE4_GRID_VERSION=stage3-current-forest-20261114\n')
        if 'ACTIVE_REGION_CODE=' not in self.config.read_text():
            self.config.write_text(self.config.read_text()+scope)
        return {'source_sha256':hashlib.sha256(self.source.read_bytes()).hexdigest(),
                'scope_revision':'a'*64,'schema_revision':'20261117_0047',
                'application_images':{'backend':'sha256:'+'b'*64,'frontend':'sha256:'+'c'*64}}

    def source_manifest(self):
        manifest = self.root/'source-manifest.json'
        manifest.write_text(json.dumps(self.manifest_value()))
        return manifest

    def test_explicit_clone_dump_matches_frozen_runtime_and_source(self):
        self.config.write_text('POSTGRES_DB="isolated_clone"\nPOSTGRES_USER=workflow_owner\n')
        manifest = self.source_manifest()
        result = self.run_job(BACKUP_POSTGRES_DB='isolated_clone', BACKUP_SOURCE_MANIFEST=str(manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root/'dump-target.json').read_text()),
                         {'database':'isolated_clone','user':'workflow_owner'})
        encrypted = next(self.backups.glob('bundle-*/configuration.tar.age')).read_bytes()
        with tarfile.open(fileobj=io.BytesIO(encrypted.split(b'\n',1)[1]),mode='r:gz') as archive:
            self.assertEqual(archive.extractfile('./runtime.env').read(), self.config.read_bytes())
            source = archive.extractfile('./source.tar.gz').read()
            expected = json.load(archive.extractfile('./source-manifest.json'))['source_sha256']
            self.assertEqual(hashlib.sha256(source).hexdigest(), expected)

    def test_sidecar_routine_dump_uses_frozen_target_not_container_defaults(self):
        self.config.write_text('POSTGRES_DB=sidecar_fixture\nPOSTGRES_USER=workflow_owner\n')
        manifest = self.source_manifest()
        result = self.run_job(BACKUP_SOURCE_MANIFEST=str(manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        dump = json.loads((self.root/'dump-target.json').read_text())
        identity = json.loads(next(self.backups.glob('bundle-*/recovery-identity.json')).read_text())
        self.assertEqual(dump['database'], 'sidecar_fixture')
        self.assertEqual(identity['runtime_target']['POSTGRES_DB'], dump['database'])

    def vpn_inputs(self):
        live=self.root/'vpn-live';build=self.root/'vpn-build';build.mkdir()
        server=['/etc/map1-ogm-vpn/client.ovpn','/etc/map1-ogm-vpn/auth.txt',
            '/etc/systemd/system/map1-ogm-openvpn-proxy.service','/opt/map1-ogm-vpn/run.sh',
            '/opt/map1-ogm-vpn/stop.py','/opt/map1-ogm-vpn/health.py']
        originals={}
        for name in server:
            target=live/name.lstrip('/');target.parent.mkdir(parents=True,exist_ok=True)
            raw=('PRIVATE-VPN-CANARY '+name).encode();target.write_bytes(raw)
            originals['vpn/server'+name]=raw
        for name in ['Dockerfile','.dockerignore','supervisor.py']:
            raw=('PRIVATE-VPN-CANARY '+name).encode();(build/name).write_bytes(raw)
            originals['vpn/source/'+name]=raw
        # Run the ACTUAL stdlib CLI with only Path("/") redirected to the
        # synthetic project tree. Production has no test-root override.
        shim=self.root/'bin/python3'
        shim.write_text('#!'+sys.executable+'\n'+r'''
import os,pathlib,runpy,sys
args=sys.argv[1:]
plain=[x for x in args if x not in ('-B','-S')]
if len(plain)>1 and pathlib.Path(plain[0]).name=='backup_offsite.py' and plain[1]=='freeze-vpn-current':
 cls=type(pathlib.Path())
 root=cls(os.environ['JOB_TEST_ROOT'])/'vpn-live'
 def project_path(*parts):
  value=cls(*parts)
  return root if str(value)=='/' else value
 pathlib.Path=project_path
 sys.argv=plain
 runpy.run_path(plain[0],run_name='__main__')
else:
 os.execv(sys.executable,[sys.executable,*args])
''')
        shim.chmod(0o700)
        return live,build,originals

    def test_daily_job_encrypts_nine_current_vpn_files_and_identity_uses_frozen_bytes(self):
        manifest=self.source_manifest()
        live,build,originals=self.vpn_inputs()
        result=self.run_job(BACKUP_SOURCE_MANIFEST=str(manifest),BACKUP_OGM_VPN_CAPTURE='1',
            BACKUP_OGM_VPN_BUILD_DIR=str(build),REPLACE_VPN_DURING_DUMP='1')
        self.assertEqual(result.returncode,0,result.stderr)
        bundle=next(self.backups.glob('bundle-*'))
        identity=json.loads((bundle/'recovery-identity.json').read_text())
        component=identity['components']['ogm_vpn']
        self.assertEqual(set(component),{'sha256','bytes','captured_at','role'})
        ciphertext=(bundle/'ogm-vpn-recovery.tar.age').read_bytes()
        self.assertEqual(component['sha256'],hashlib.sha256(ciphertext).hexdigest())
        self.assertEqual(component['bytes'],len(ciphertext))
        self.assertLessEqual(datetime.fromisoformat(component['captured_at']),
                             datetime.fromisoformat((bundle/'snapshot-at.txt').read_text().strip()))
        self.assertNotIn('auth.txt',json.dumps(identity))
        self.assertNotIn('client.ovpn',json.dumps(identity))
        self.assertNotEqual((live/'etc/map1-ogm-vpn/client.ovpn').read_bytes(),
                            originals['vpn/server/etc/map1-ogm-vpn/client.ovpn'])
        encrypted=(bundle/'ogm-vpn-recovery.tar.age').read_bytes()
        with tarfile.open(fileobj=io.BytesIO(encrypted.split(b'\n',1)[1]),mode='r:gz') as archive:
            for name,raw in originals.items():self.assertEqual(archive.extractfile('./'+name.removeprefix('vpn/')).read(),raw)
            member_manifest=json.load(archive.extractfile('./manifest.json'))
            self.assertEqual(member_manifest['files'],{name:hashlib.sha256(raw).hexdigest() for name,raw in originals.items()})
            self.assertIn('./restore-instructions.txt',archive.getnames())
        self.assertNotIn('PRIVATE-VPN-CANARY',json.dumps(identity))
        self.assertFalse((bundle/'configuration').exists())
        self.assertEqual(set(x.name for x in bundle.iterdir() if x.suffix=='.age'),
                         {'configuration.tar.age','ogm-vpn-recovery.tar.age',next(bundle.glob('*.dump.age')).name})

    def test_missing_or_oversized_current_vpn_input_stops_before_dump(self):
        manifest=self.source_manifest();live,build,_=self.vpn_inputs()
        source=live/'etc/map1-ogm-vpn/client.ovpn';source.unlink()
        params=dict(BACKUP_SOURCE_MANIFEST=str(manifest),BACKUP_OGM_VPN_CAPTURE='1',
                    BACKUP_OGM_VPN_BUILD_DIR=str(build))
        result=self.run_job(**params)
        self.assertNotEqual(result.returncode,0)
        source.write_bytes(b'x'*200001)
        result=self.run_job(**params)
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn('dump',(self.root/'calls').read_text().splitlines())
        self.assertFalse(list(self.backups.glob('bundle-*')))

    def test_partial_capture_configuration_and_historical_input_not_silently_promoted(self):
        for changes in [{'BACKUP_OGM_VPN_CAPTURE':'1'},{'BACKUP_OGM_VPN_SHA256':'0'*64}]:
            with self.subTest(changes=changes):
                result=self.run_job(**changes)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse((self.root/'calls').exists())

    def test_explicit_target_mismatch_unsafe_or_ambiguous_settings_fail_before_transfer(self):
        manifest = self.source_manifest()
        for changes in ({'BACKUP_POSTGRES_DB':'other_database'}, {'BACKUP_POSTGRES_USER':'other_user'},
                        {'BACKUP_POSTGRES_DB':'--secret-canary'}, {'BACKUP_POSTGRES_USER':'name;true'}):
            result = self.run_job(BACKUP_SOURCE_MANIFEST=str(manifest), **changes)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root/'calls').exists())
            for value in changes.values():self.assertNotIn(value, result.stdout + result.stderr)
        self.config.write_text('POSTGRES_DB=workflow_fixture\nPOSTGRES_DB=workflow_fixture\nPOSTGRES_USER=workflow_owner\n')
        result = self.run_job(BACKUP_POSTGRES_DB='workflow_fixture', BACKUP_SOURCE_MANIFEST=str(manifest))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'calls').exists())

    def test_explicit_target_requires_source_manifest_before_transfer(self):
        result = self.run_job(BACKUP_POSTGRES_DB='workflow_fixture')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'calls').exists())

    def test_explicit_target_is_rechecked_after_runtime_configuration_freezes(self):
        result = self.run_job(BACKUP_POSTGRES_DB='workflow_fixture',
                              BACKUP_SOURCE_MANIFEST=str(self.source_manifest()),
                              REPLACE_RUNTIME_DURING_PREFLIGHT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root/'calls').read_text().splitlines(), ['preflight'])
        self.assertEqual(list(self.backups.glob('bundle-*')), [])

    def test_explicit_target_does_not_republish_unidentified_pending_bundle(self):
        pending = self.prior('bundle-20261002T020000Z-EfGh5678',False)
        result = self.run_job(BACKUP_POSTGRES_DB='workflow_fixture',
                              BACKUP_SOURCE_MANIFEST=str(self.source_manifest()))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'calls').exists())
        self.assertTrue(pending.exists())


if __name__ == '__main__':
    unittest.main()
