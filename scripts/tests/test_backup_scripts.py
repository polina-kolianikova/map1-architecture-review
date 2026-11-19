"""Local workflow regressions: all Docker/age calls are fake; no DB is contacted.

Run: python3 -B -m unittest discover -s scripts/tests -p 'test_backup_scripts.py' -v
"""

import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1]
FAKE_TOOL = r'''
import json, os, pathlib, sys, time

tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
env = os.environ
root = pathlib.Path(env['FAKE_ROOT'])

def record(action, **extra):
    with (root / 'calls.jsonl').open('a') as log:
        log.write(json.dumps(dict(action=action, args=args, **extra)) + '\n')

if tool == 'docker':
    if 'compose' in args and 'ps' in args:
        record('resolve')
        print(env.get('RESOLVED_CONTAINER', 'a' * 64))
        sys.exit(0)
    if args[0] == 'inspect':
        record('inspect')
        print('|'.join([env.get('INSPECT_ID', 'a' * 64), env.get('INSPECT_RUNNING', 'true'),
                        env.get('INSPECT_PROJECT', 'map1-restore'), env.get('INSPECT_SERVICE', 'db'),
                        env.get('INSPECT_ISOLATED', 'true')]))
        sys.exit(0)
    script = args[args.index('sh') + 2]
    if 'pg_dump ' in script:
        container = dict(env, POSTGRES_USER='container_owner', POSTGRES_DB='container_main')
        os.execve('/bin/sh', args[args.index('sh'):], container)
    if 'pg_restore --list' in script:
        data = sys.stdin.buffer.read()
        record('preflight')
        sys.exit(1 if env.get('PREFLIGHT_FAIL') or not data.startswith(b'PGDMP') else 0)
    if 'createdb ' in script:
        record('create', db=args[-1])
        sys.exit(int(env.get('CREATE_EXIT', '0')))
    if 'dropdb ' in script:
        record('drop', db=args[-1])
        sys.exit(int(env.get('DROP_EXIT', '0')))
    if 'pg_restore ' in script:
        record('restore', db=args[-1], size=len(sys.stdin.buffer.read()))
        sys.exit(int(env.get('RESTORE_EXIT', '0')))
    if 'psql ' in script:
        record('validate', db=args[-1], sql=sys.stdin.read())
        print('grid_cells=0\nverification_results=0\nverification_audit_events=0')
        print('grid_cells_partitioned=' + env.get('PARTITION_COUNT', '0'))
        sys.exit(int(env.get('VALIDATE_EXIT', '0')))
    raise SystemExit('unexpected docker invocation')
if tool == 'pg_dump':
    record('dump', user=args[args.index('-U') + 1], db=args[args.index('-d') + 1])
    (root / 'dump-ready').touch()
    if env.get('DUMP_WAIT'):
        while not (root / 'release-dump').exists():
            time.sleep(0.01)
    if not env.get('DUMP_EMPTY'):
        sys.stdout.buffer.write(b'PGDMP\x00bounded fixture\x00')
    sys.exit(int(env.get('DUMP_EXIT', '0')))
if tool == 'age':
    if '-d' in args:
        record('decrypt')
        data = pathlib.Path(args[-1]).read_bytes()
        if not data.startswith(b'age-encryption.org/v1\n'):
            sys.exit(1)
        sys.stdout.buffer.write(data.split(b'\n', 1)[1])
        sys.exit(int(env.get('DECRYPT_EXIT', '0')))
    record('encrypt')
    data = sys.stdin.buffer.read()
    pathlib.Path(args[args.index('-o') + 1]).write_bytes(b'age-encryption.org/v1\n' + data)
    sys.exit(int(env.get('ENCRYPT_EXIT', '0')))
if tool == 'sha256sum':
    record('checksum')
    if env.get('CHECKSUM_EXIT'):
        print('partial checksum')
        sys.exit(1)
    os.execv('/usr/bin/sha256sum', ['sha256sum'] + args)
if tool == 'mv':
    record('publish', visible=[p.name for p in pathlib.Path(env['BACKUP_DIR']).glob('*.dump*')])
    is_checksum = args[-1].endswith('.sha256')
    if env.get('PUBLISH_FAIL') == ('checksum' if is_checksum else 'archive'):
        sys.exit(1)
    os.execv('/usr/bin/mv', ['mv'] + args)
if tool == 'date':
    print('20261029-123456')
    sys.exit(0)
raise SystemExit('unexpected tool: ' + tool)
'''


class BackupScriptsTests(unittest.TestCase):
    def setUp(self):
        # Keep every test artifact inside the authorized project tree.
        self.temp = tempfile.TemporaryDirectory(prefix='.backup-test-', dir=SCRIPTS / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'fake-tool'
        fake.write_text(f'#!{sys.executable}\n' + FAKE_TOOL)
        fake.chmod(0o700)
        for tool in ('docker', 'pg_dump', 'age', 'sha256sum', 'mv', 'date'):
            (self.bin / tool).symlink_to(fake)
        self.backups = self.root / 'backups'
        self.identity = self.root / 'identity.txt'
        self.identity.write_text('fake identity')
        self.env = {
            'PATH': f'{self.bin}:/usr/bin:/bin',
            'FAKE_ROOT': str(self.root),
            'TMPDIR': str(self.root),
            'ENV_FILE': str(self.root / 'no-env'),
            'COMPOSE_FILE': 'infra/docker-compose.prod.yml',
            'DB_SERVICE': 'db',
            'RESTORE_COMPOSE_PROJECT': 'map1-restore',
            'RESTORE_EXPECTED_CONTAINER_ID': 'a' * 64,
            'BACKUP_DIR': str(self.backups),
            'BACKUP_REQUIRE_ENCRYPTION': '1',
            'BACKUP_AGE_RECIPIENT': 'fake-recipient',
            'BACKUP_AGE_IDENTITY': str(self.identity),
            'BACKUP_RETENTION_DAYS': '30',
            'ALLOW_PLAINTEXT_BACKUP': '0',
        }

    def run_script(self, script, *args, **changes):
        return subprocess.run(
            ['/bin/bash', str(SCRIPTS / script), *map(str, args)],
            cwd=SCRIPTS.parent, env=dict(self.env, **changes),
            capture_output=True, text=True, timeout=10,
        )

    def calls(self):
        log = self.root / 'calls.jsonl'
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def actions(self):
        return [call['action'] for call in self.calls()]

    def assert_failed_cleanly(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(list(self.backups.glob('*.dump*')), [])
        self.assertEqual([p for p in self.backups.glob('.backup.*') if p.is_dir()], [])

    def archive(self, name='fixture.dump.age'):
        self.backups.mkdir(exist_ok=True)
        path = self.backups / name
        data = b'PGDMP\x00bounded fixture'
        path.write_bytes(b'age-encryption.org/v1\n' + data if name.endswith('.age') else data)
        self.checksum(path)
        return path

    def checksum(self, path, entry=None):
        checksum = Path(str(path) + '.sha256')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        checksum.write_text(entry if entry is not None else f'{digest}  {path.name}\n')
        return checksum

    def test_encrypted_publish_is_complete_private_and_stdout_is_path_only(self):
        result = self.run_script('backup-db.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(result.stdout.strip())
        self.assertEqual(result.stdout, str(path) + '\n')
        self.assertEqual(path.parent, self.backups)
        self.assertTrue(path.name.endswith('.dump.age'))
        self.assertTrue(path.read_bytes().startswith(b'age-encryption.org/v1\nPGDMP'))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)
        sidecar = Path(str(path) + '.sha256')
        self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)
        self.assertEqual(sidecar.read_text(), hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n')
        publish = [c for c in self.calls() if c['action'] == 'publish']
        self.assertEqual(publish[0]['visible'], [])
        self.assertEqual(publish[1]['visible'], [sidecar.name])
        dump_call = next(call for call in self.calls() if call['action'] == 'dump')
        self.assertIn('--no-tablespaces', ' '.join(dump_call['args']))
        self.assertEqual([p for p in self.backups.glob('.backup.*') if p.is_dir()], [])

    def test_partial_dump_encryption_checksum_and_publish_failures_leave_no_artifact(self):
        for failure in (
            {'DUMP_EXIT': '7'}, {'DUMP_EMPTY': '1'}, {'ENCRYPT_EXIT': '4'},
            {'CHECKSUM_EXIT': '1'}, {'PUBLISH_FAIL': 'checksum'}, {'PUBLISH_FAIL': 'archive'},
        ):
            with self.subTest(failure=failure):
                self.assert_failed_cleanly(self.run_script('backup-db.sh', **failure))

    def test_default_dump_uses_existing_container_not_runtime_file_or_host_selectors(self):
        config = self.root / 'different.env'
        config.write_text('POSTGRES_DB=file_database\nPOSTGRES_USER=file_owner\n')
        result = self.run_script('backup-db.sh', ENV_FILE=str(config),
                                 POSTGRES_DB='host_database', POSTGRES_USER='host_owner')
        self.assertEqual(result.returncode, 0, result.stderr)
        call = next(c for c in self.calls() if c['action'] == 'dump')
        self.assertEqual((call['db'], call['user']), ('container_main', 'container_owner'))

    def test_explicit_selectors_reach_pg_dump_as_separate_arguments(self):
        for overrides, expected in (
            ({'BACKUP_POSTGRES_DB': 'map1_rehearsal_20261101'}, ('map1_rehearsal_20261101', 'container_owner')),
            ({'BACKUP_POSTGRES_USER': 'backup_reader'}, ('container_main', 'backup_reader')),
            ({'BACKUP_POSTGRES_DB': 'd' * 63, 'BACKUP_POSTGRES_USER': 'u' * 63}, ('d' * 63, 'u' * 63)),
        ):
            with self.subTest(overrides=overrides):
                result = self.run_script('backup-db.sh', **overrides)
                self.assertEqual(result.returncode, 0, result.stderr)
                call = [c for c in self.calls() if c['action'] == 'dump'][-1]
                self.assertEqual((call['db'], call['user']), expected)
                self.assertEqual(result.stdout, str(Path(result.stdout.strip())) + '\n')

    def test_invalid_explicit_selectors_fail_without_shell_execution_or_value_logging(self):
        marker = self.root / 'injection-marker'
        for key in ('BACKUP_POSTGRES_DB', 'BACKUP_POSTGRES_USER'):
            for value in ('--help', 'db name', 'db\nother', 'db;true', 'x' * 64,
                          'db/other', 'secret-canary-' + str(marker), '$(touch ' + str(marker) + ')'):
                with self.subTest(key=key, value=value):
                    result = self.run_script('backup-db.sh', **{key: value})
                    self.assert_failed_cleanly(result)
                    self.assertNotIn(value, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertNotIn('dump', self.actions())

    def test_partial_plaintext_dump_is_also_removed(self):
        self.assert_failed_cleanly(self.run_script(
            'backup-db.sh', BACKUP_AGE_RECIPIENT='', BACKUP_REQUIRE_ENCRYPTION='0',
            ALLOW_PLAINTEXT_BACKUP='1', DUMP_EXIT='1',
        ))

    def test_same_second_backups_have_distinct_names_and_keep_lock_inode(self):
        first = self.run_script('backup-db.sh')
        inode = (self.backups / '.backup.lock').stat().st_ino
        second = self.run_script('backup-db.sh')
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertNotEqual(first.stdout, second.stdout)
        self.assertEqual((self.backups / '.backup.lock').stat().st_ino, inode)
        self.assertTrue(Path(first.stdout.strip()).exists())

    def test_unsafe_parameters_fail_before_dump(self):
        for changes in (
            {'BACKUP_RETENTION_DAYS': '-1'}, {'BACKUP_RETENTION_DAYS': '1.5'},
            {'BACKUP_RETENTION_DAYS': '999999999999999999999999'},
            {'BACKUP_REQUIRE_ENCRYPTION': 'false'}, {'ALLOW_PLAINTEXT_BACKUP': 'yes'},
            {'DB_SERVICE': '--privileged'}, {'BACKUP_DIR': '/'},
            {'BACKUP_DIR': str(SCRIPTS.parent)},
            {'BACKUP_AGE_RECIPIENT': ''},
            {'BACKUP_AGE_RECIPIENT': '', 'ALLOW_PLAINTEXT_BACKUP': '1'},
            {'BACKUP_AGE_RECIPIENT': '', 'BACKUP_REQUIRE_ENCRYPTION': '0', 'COMPOSE_FILE': './infra/docker-compose.prod.yml'},
        ):
            with self.subTest(changes=changes):
                self.assertNotEqual(self.run_script('backup-db.sh', **changes).returncode, 0)
        self.assertNotIn('dump', self.actions())

    def test_missing_checksum_executable_fails_closed(self):
        (self.bin / 'sha256sum').unlink()
        result = self.run_script('backup-db.sh', PATH=str(self.bin))
        self.assert_failed_cleanly(result)
        self.assertIn('sha256sum is required', result.stderr)

    def test_symlink_output_directory_and_lock_are_rejected(self):
        self.backups.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(self.backups, target_is_directory=True)
        self.assertNotEqual(self.run_script('backup-db.sh', BACKUP_DIR=str(alias)).returncode, 0)
        sentinel = self.root / 'sentinel'
        sentinel.write_text('unchanged')
        (self.backups / '.backup.lock').symlink_to(sentinel)
        self.assertNotEqual(self.run_script('backup-db.sh').returncode, 0)
        self.assertEqual(sentinel.read_text(), 'unchanged')
        self.assertNotIn('dump', self.actions())

    def test_retention_only_prunes_own_top_level_timestamped_pairs(self):
        old = self.archive('map1-turkey-20200101-010101.dump.age')
        nested = self.backups / 'nested'
        nested.mkdir()
        nested_old = nested / old.name
        nested_old.write_bytes(b'keep nested')
        unrelated = self.backups / 'map1-turkey-not-a-timestamp.dump.age'
        unrelated.write_bytes(b'keep unrelated')
        for path in (old, nested_old, unrelated):
            os.utime(path, (1, 1))
        result = self.run_script('backup-db.sh', BACKUP_RETENTION_DAYS='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertFalse(Path(str(old) + '.sha256').exists())
        self.assertTrue(nested_old.exists())
        self.assertTrue(unrelated.exists())

    def test_concurrent_backup_is_rejected_and_term_cleans_staging(self):
        proc = subprocess.Popen(
            ['/bin/bash', str(SCRIPTS / 'backup-db.sh')], cwd=SCRIPTS.parent,
            env=dict(self.env, DUMP_WAIT='1'), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'dump-ready').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / 'dump-ready').exists())
            second = self.run_script('backup-db.sh')
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('another backup is running', second.stderr)
            self.assertEqual(self.actions().count('dump'), 1)
        finally:
            os.killpg(proc.pid, signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=5)
        self.assert_failed_cleanly(subprocess.CompletedProcess([], proc.returncode, stdout, stderr))
        self.assertEqual(self.run_script('backup-db.sh').returncode, 0)

    def test_checksum_cannot_validate_another_file_or_multiple_entries(self):
        path = self.archive()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for entry in (
            f'{digest}  other.dump.age\n', f'{digest}  ../{path.name}\n',
            f'{digest}  /tmp/{path.name}\n', f'{digest}  {path.name}\n{digest}  {path.name}\n',
            f'{digest}  {path.name}\n\n', 'malformed\n', '',
        ):
            with self.subTest(entry=entry):
                self.checksum(path, entry)
                result = self.run_script('check-backup-restore.sh', path)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_missing_or_mismatched_checksum_fails_before_any_docker_call(self):
        path = self.archive('renamed.dump')
        Path(str(path) + '.sha256').unlink()
        self.assertNotEqual(self.run_script('check-backup-restore.sh', path).returncode, 0)
        self.checksum(path)
        path.write_bytes(b'PGDMPcorrupted')
        self.assertNotEqual(self.run_script('check-backup-restore.sh', path).returncode, 0)
        self.assertEqual(self.actions(), ['checksum'])

    def test_decryption_and_toc_failures_happen_before_database_creation(self):
        path = self.archive()
        for changes in ({'DECRYPT_EXIT': '1'}, {'PREFLIGHT_FAIL': '1'}, {'BACKUP_AGE_IDENTITY': ''}):
            with self.subTest(changes=changes):
                result = self.run_script('check-backup-restore.sh', path, **changes)
                self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('create', self.actions())
        self.assertNotIn('drop', self.actions())

    def test_restore_requires_explicit_isolated_identity_before_docker(self):
        path = self.archive()
        for key in ('COMPOSE_FILE', 'DB_SERVICE', 'RESTORE_COMPOSE_PROJECT', 'RESTORE_EXPECTED_CONTAINER_ID'):
            with self.subTest(key=key):
                result = self.run_script('check-backup-restore.sh', path, **{key: ''})
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_restore_rejects_wrong_stopped_unlabelled_or_ambiguous_container(self):
        path = self.archive()
        for changes in ({'RESOLVED_CONTAINER': 'b' * 64}, {'RESOLVED_CONTAINER': ''},
                        {'RESOLVED_CONTAINER': 'a' * 64 + '\n' + 'b' * 64},
                        {'INSPECT_ID': 'b' * 64}, {'INSPECT_RUNNING': 'false'},
                        {'INSPECT_PROJECT': 'main'}, {'INSPECT_SERVICE': 'backend'},
                        {'INSPECT_ISOLATED': '<no value>'}):
            with self.subTest(changes=changes):
                result = self.run_script('check-backup-restore.sh', path, **changes)
                self.assertNotEqual(result.returncode, 0)
        for forbidden in ('preflight', 'create', 'restore', 'drop'):
            self.assertNotIn(forbidden, self.actions())

    def test_override_is_used_for_resolution_and_all_operations_pin_container_socket(self):
        result = self.run_script('check-backup-restore.sh', self.archive(),
                                 RESTORE_COMPOSE_OVERRIDE_FILE='/isolated/restore.override.yml')
        self.assertEqual(result.returncode, 0, result.stderr)
        resolve = next(call for call in self.calls() if call['action'] == 'resolve')
        self.assertIn('/isolated/restore.override.yml', resolve['args'])
        self.assertEqual(resolve['args'][resolve['args'].index('--project-name') + 1], 'map1-restore')
        self.assertEqual(self.actions().count('resolve'), 1)
        for call in self.calls():
            if call['action'] in ('preflight', 'create', 'restore', 'validate', 'drop'):
                self.assertEqual(call['args'][:3], ['exec', '-i', 'a' * 64])
            if call['action'] in ('create', 'restore', 'validate', 'drop'):
                self.assertIn('-h /var/run/postgresql -p 5432', ' '.join(call['args']))

    def test_empty_and_partitioned_restored_counts_are_accepted_after_validation(self):
        path = self.archive()
        for count in ('0', '42'):
            with self.subTest(partition_count=count):
                result = self.run_script('check-backup-restore.sh', path, PARTITION_COUNT=count)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('backup restore check passed', result.stdout)
                self.assertIn('no source row-count comparison', result.stdout)
        calls = self.calls()
        actions = self.actions()
        self.assertLess(actions.index('checksum'), actions.index('preflight'))
        self.assertLess(actions.index('preflight'), actions.index('create'))
        self.assertEqual(actions.count('create'), actions.count('drop'))
        created = [c['db'] for c in calls if c['action'] == 'create']
        self.assertEqual(len(set(created)), 2)
        for call in calls:
            if call['action'] == 'restore':
                command = ' '.join(call['args'])
                for option in ('--single-transaction', '--no-owner', '--no-privileges', '--no-tablespaces'):
                    self.assertIn(option, command)
            if call['action'] in ('create', 'restore', 'validate', 'drop'):
                self.assertRegex(call['db'], r'^map1_restore_check_[A-Za-z0-9]{12}$')
            if call['action'] == 'validate':
                self.assertEqual(call['sql'], (SCRIPTS / 'backup-restore-validation.sql').read_text())
                self.assertIn('-X', ' '.join(call['args']))

    def test_plaintext_binary_marker_checksum_is_supported(self):
        path = self.archive('fixture.dump')
        self.checksum(path, hashlib.sha256(path.read_bytes()).hexdigest().upper() + f' *{path.name}\n')
        result = self.run_script('check-backup-restore.sh', path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('decrypt', self.actions())

    def test_create_failure_does_not_drop_a_database_we_did_not_create(self):
        result = self.run_script('check-backup-restore.sh', self.archive(), CREATE_EXIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('drop', self.actions())
        self.assertEqual(list(self.root.glob('map1-restore-check.*')), [])

    def test_restore_or_sql_failure_cleans_database_and_does_not_report_success(self):
        path = self.archive()
        for changes in ({'RESTORE_EXIT': '1'}, {'VALIDATE_EXIT': '1'}):
            with self.subTest(changes=changes):
                result = self.run_script('check-backup-restore.sh', path, **changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('check passed', result.stdout)
        self.assertEqual(self.actions().count('create'), self.actions().count('drop'))

    def test_failed_database_cleanup_is_reported_as_failure(self):
        result = self.run_script('check-backup-restore.sh', self.archive(), RESTORE_EXIT='1', DROP_EXIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('failed to remove temporary restore database', result.stderr)
        result = self.run_script('check-backup-restore.sh', self.archive(), DROP_EXIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('check passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
