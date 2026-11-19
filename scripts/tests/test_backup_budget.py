"""Real local child/group tests; no Docker daemon, database or network calls."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('backup_budget', SCRIPTS / 'backup_budget.py')
budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(budget)


class BackupBudgetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.backup-budget-', dir=SCRIPTS / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def command(self, code, *args):
        return [sys.executable, '-B', '-c', code, *map(str, args)]

    def cli(self, command, *options):
        return [sys.executable, '-B', str(SCRIPTS / 'backup_budget.py'),
                '--directory', str(self.root), '--min-free-bytes', '0',
                '--min-free-inodes', '0', '--max-file-bytes', str(1024**2),
                '--interval-seconds', '0.05', '--grace-seconds', '0.15',
                *options, '--', *command]

    def launch(self, command):
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        self.addCleanup(self.finish, child)
        return child

    @staticmethod
    def finish(child):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        if child.stdout:
            child.stdout.close()
        if child.stderr:
            child.stderr.close()

    def wait_file(self, path):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return path.read_text()
            time.sleep(0.01)
        self.fail('owned fixture did not become ready')

    @staticmethod
    def alive(pid):
        # A killed orphan can briefly be a zombie until init reaps it.
        try:
            return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
        except FileNotFoundError:
            return False

    def test_actual_filesystem_probe_and_exact_threshold_boundaries(self):
        budget.check_reserve(self.root, 0, 0)
        info = SimpleNamespace(f_bavail=10, f_frsize=4096, f_favail=100000)
        with mock.patch.object(budget.os, 'statvfs', return_value=info):
            budget.check_reserve(self.root, 40960, 100000)
            for free, inodes in [(40961, 100000), (40960, 100001)]:
                with self.assertRaises(budget.BudgetError):
                    budget.check_reserve(self.root, free, inodes)

    def test_insufficient_bytes_or_inodes_never_starts_child(self):
        marker = self.root / 'must-not-start'
        command = self.command('from pathlib import Path;import sys;Path(sys.argv[1]).touch()', marker)
        for option in ['--min-free-bytes', '--min-free-inodes']:
            result = subprocess.run(self.cli(command, option, str(2**60)), capture_output=True,
                                    text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    def test_unreadable_probe_fails_without_starting_or_printing_path(self):
        marker = self.root / 'must-not-start'
        missing = self.root / 'private-path-value'
        result = subprocess.run(self.cli(self.command('raise SystemExit(0)'),
            '--directory', str(missing)), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('private-path-value', result.stderr)
        self.assertFalse(marker.exists())

    def test_success_and_child_failure_are_propagated(self):
        for code in [0, 7]:
            result = subprocess.run(self.cli(self.command(f'raise SystemExit({code})')),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, code, result.stderr)

    def test_multiple_explicit_paths_with_spaces_and_missing_path(self):
        extra = self.root / 'database volume'
        extra.mkdir()
        marker = self.root / 'child-ran'
        command = self.command('from pathlib import Path;import sys;Path(sys.argv[1]).touch()', marker)
        result = subprocess.run(self.cli(command, '--reserve-path', str(extra),
            '--reserve-path', str(self.root)), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        marker.unlink()
        extra.rmdir()
        result = subprocess.run(self.cli(command, '--reserve-path', str(extra)),
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertNotIn(str(extra), result.stderr)

    def test_only_additional_filesystem_breach_stops_running_child(self):
        extra = self.root / 'pgdata'
        extra.mkdir()
        actual_statvfs = os.statvfs
        for index in (4, 7):
            marker = self.root / f'extra-breach-{index}'
            def sample(path):
                values = list(actual_statvfs(path))
                if Path(path) == extra and marker.exists():
                    values[index] = 0
                return os.statvfs_result(values)
            code = 'import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)'
            with mock.patch.object(budget.os, 'statvfs', side_effect=sample), contextlib.redirect_stderr(io.StringIO()):
                result = budget.run_guard(self.command(code, marker), directory=self.root,
                    reserve_paths=[str(extra)], min_free_bytes=1, min_free_inodes=1,
                    max_file_bytes=1024**2, interval_seconds=0.02, grace_seconds=0.15)
            self.assertEqual(result, 1)
            self.assertFalse(self.alive(int(marker.read_text())))

    def test_additional_filesystem_replacement_fails_even_with_free_space(self):
        extra = self.root / 'pgdata'
        extra.mkdir()
        marker = self.root / 'mount-changed'
        real_stat = os.stat
        def sample(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if Path(path) == extra and marker.exists():
                values = list(result)
                values[2] += 1  # st_dev; simulate a mount replaced by another FS.
                return os.stat_result(values)
            return result
        code = 'import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)'
        with mock.patch.object(budget.os, 'stat', side_effect=sample), contextlib.redirect_stderr(io.StringIO()):
            result = budget.run_guard(self.command(code, marker), directory=self.root,
                reserve_paths=[str(extra)], min_free_bytes=0, min_free_inodes=0,
                max_file_bytes=1024**2, interval_seconds=0.02, grace_seconds=0.15)
        self.assertEqual(result, 1)
        self.assertFalse(self.alive(int(marker.read_text())))

    def test_file_size_limit_is_inherited_and_really_limits_output(self):
        output = self.root / 'bounded-output'
        code = '''import os,signal,sys
signal.signal(signal.SIGXFSZ,signal.SIG_DFL)
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT,0o600)
os.write(fd,b'x'*8192)
os.write(fd,b'x')
'''
        result = subprocess.run(self.cli(self.command(code, output), '--max-file-bytes', '4096'),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 128 + signal.SIGXFSZ)
        self.assertEqual(output.stat().st_size, 4096)

    def test_existing_lower_hard_file_limit_is_not_raised(self):
        command = self.cli(self.command('import resource;print(resource.getrlimit(resource.RLIMIT_FSIZE))'),
                           '--max-file-bytes', '4096')
        wrapper = self.command('import os,resource,sys;resource.setrlimit(resource.RLIMIT_FSIZE,(2048,2048));os.execv(sys.executable,[sys.executable,*sys.argv[1:]])',
                               *command[1:])
        result = subprocess.run(wrapper,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '(2048, 2048)')

    def test_continuing_byte_and_inode_breaches_stop_real_running_children(self):
        actual_statvfs = os.statvfs
        for index in [4, 7]:  # statvfs f_bavail and f_favail, respectively.
            with self.subTest(stat_index=index):
                marker = self.root / f'running-{index}'
                code = 'import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)'
                def sample(path):
                    values = list(actual_statvfs(path))
                    if marker.exists():
                        values[index] = 0
                    return os.statvfs_result(values)
                with mock.patch.object(budget.os, 'statvfs', side_effect=sample), contextlib.redirect_stderr(io.StringIO()):
                    result = budget.run_guard(self.command(code, marker), directory=self.root,
                        min_free_bytes=1, min_free_inodes=1, max_file_bytes=1024**2,
                        interval_seconds=0.02, grace_seconds=0.15)
                self.assertEqual(result, 1)
                self.assertTrue(marker.exists())
                self.assertFalse(self.alive(int(marker.read_text())))

    def test_final_probe_rejects_short_success_after_reserve_drops(self):
        calls = []
        def sample(*args):
            calls.append(args)
            if len(calls) == 3:
                raise budget.BudgetError('Final reserve breached')
            # Hold the first post-launch sample until the child has exited, so
            # the next sample must be the final check, not the periodic check.
            if len(calls) == 2:
                time.sleep(0.2)
        with mock.patch.object(budget, 'check_reserve', side_effect=sample), contextlib.redirect_stderr(io.StringIO()):
            result = budget.run_guard(self.command('pass'), directory=self.root,
                min_free_bytes=0, min_free_inodes=0, max_file_bytes=1024**2,
                interval_seconds=1, grace_seconds=0.15)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 3)

    def test_signals_are_forwarded_and_unrelated_process_survives(self):
        unrelated = self.launch(self.command('import time;time.sleep(30)'))
        for signum in [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]:
            with self.subTest(signum=signum):
                ready, caught = self.root / f'ready-{signum}', self.root / f'caught-{signum}'
                code = '''import os,signal,sys,time
from pathlib import Path
def stop(signum,frame):
 Path(sys.argv[2]).write_text(str(signum))
 raise SystemExit(0)
for signum in [signal.SIGINT,signal.SIGTERM,signal.SIGHUP]:signal.signal(signum,stop)
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
'''
                guard = self.launch(self.cli(self.command(code, ready, caught)))
                pid = int(self.wait_file(ready))
                self.assertNotEqual(os.getpgid(pid), os.getpgid(unrelated.pid))
                guard.send_signal(signum)
                _, stderr = guard.communicate(timeout=5)
                self.assertEqual(guard.returncode, 128 + signum, stderr)
                self.assertEqual(int(caught.read_text()), signum)
                self.assertFalse(self.alive(pid))
                self.assertIsNone(unrelated.poll())

    def test_grace_then_kill_covers_owned_descendant_only(self):
        unrelated = self.launch(self.command('import time;time.sleep(30)'))
        ready = self.root / 'stubborn-group'
        code = '''import json,os,signal,subprocess,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'])
Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(30)
'''
        guard = self.launch(self.cli(self.command(code, ready)))
        pids = json.loads(self.wait_file(ready))
        self.assertEqual(os.getpgid(pids[1]), pids[0])
        started = time.monotonic()
        guard.terminate()
        _, stderr = guard.communicate(timeout=5)
        self.assertEqual(guard.returncode, 143, stderr)
        self.assertGreaterEqual(time.monotonic()-started, 0.1)
        self.assertLess(time.monotonic()-started, 3)
        self.assertTrue(all(not self.alive(pid) for pid in pids))
        self.assertIsNone(unrelated.poll())

    def test_probe_failure_during_job_stops_owned_child(self):
        marker = self.root / 'probe-failure-ready'
        code = 'import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)'
        actual_statvfs = os.statvfs
        def sample(path):
            if marker.exists():
                raise OSError('private filesystem error detail')
            return actual_statvfs(path)
        errors = io.StringIO()
        with mock.patch.object(budget.os, 'statvfs', side_effect=sample), contextlib.redirect_stderr(errors):
            result = budget.run_guard(self.command(code, marker), directory=self.root,
                min_free_bytes=0, min_free_inodes=0, max_file_bytes=1024**2,
                interval_seconds=0.02, grace_seconds=0.15)
        self.assertEqual(result, 1)
        self.assertFalse(self.alive(int(marker.read_text())))
        self.assertNotIn('private filesystem error detail', errors.getvalue())

    def test_invalid_budgets_and_missing_command_fail_before_start(self):
        for options in [dict(max_file_bytes=0),dict(min_free_bytes=-1),
                        dict(interval_seconds=1.01),dict(interval_seconds=float('nan')),
                        dict(grace_seconds=float('inf'))]:
            with self.assertRaises(ValueError):
                budget.run_guard(self.command('pass'),**{**dict(directory=self.root,
                    min_free_bytes=0,min_free_inodes=0,max_file_bytes=1024),**options})
        with self.assertRaises(ValueError):
            budget.run_guard([],directory=self.root,min_free_bytes=0,min_free_inodes=0,max_file_bytes=1024)

    def test_offsite_reserve_failure_uses_stage_cleanup_preserves_previous_bundle(self):
        spec = importlib.util.spec_from_file_location('offsite_fixture',SCRIPTS/'tests/test_backup_offsite_job.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        case = module.OffsiteJobTest()
        case.setUp()
        self.addCleanup(case.doCleanups)
        previous = case.prior('bundle-20261001T020000Z-AbCd1234', True)
        result = case.run_job(BACKUP_MIN_FREE_INODES=str(2**60))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('inode reserve breached', result.stderr)
        self.assertTrue(previous.exists())
        self.assertEqual((previous/'retained.age').read_bytes(),b'older recovery copy')
        self.assertFalse(list(case.backups.glob('.bundle-*')))
        self.assertEqual((case.root/'calls').read_text().splitlines(),['preflight'])

    def test_offsite_continuing_breach_removes_only_stage_and_never_uploads(self):
        spec = importlib.util.spec_from_file_location('offsite_fixture',SCRIPTS/'tests/test_backup_offsite_job.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        case = module.OffsiteJobTest()
        case.setUp()
        self.addCleanup(case.doCleanups)
        previous = case.prior('bundle-20261001T020000Z-AbCd1234',True)
        # A real pg_dump fixture writes dump-target.json. Only the guard's
        # filesystem sample is injected; the wrapper, child pipeline, signals,
        # nonzero exit and shell staging cleanup all execute normally.
        shim = case.root/'bin/python3'
        shim.write_text(f'#!{sys.executable}\n'+r'''
import os,pathlib,runpy,sys
if pathlib.Path(sys.argv[1]).name != 'backup_budget.py':
 os.execv(sys.executable,[sys.executable,*sys.argv[1:]])
actual=os.statvfs
def sample(path):
 values=list(actual(path))
 if (pathlib.Path(os.environ['JOB_TEST_ROOT'])/'dump-target.json').exists():values[7]=0
 return os.statvfs_result(values)
os.statvfs=sample
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
''')
        shim.chmod(0o700)
        result = case.run_job()
        self.assertNotEqual(result.returncode,0)
        self.assertIn('inode reserve breached',result.stderr)
        self.assertEqual((case.root/'calls').read_text().splitlines(),['preflight','dump'])
        self.assertTrue(previous.exists())
        self.assertEqual(list(case.backups.glob('bundle-*')),[previous])
        self.assertFalse(list(case.backups.glob('.bundle-*')))


if __name__ == '__main__':
    unittest.main()
