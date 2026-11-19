"""Exercise the shell lifecycle using fake Docker/curl; no daemon or network."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'run-stage3-tool-host-network.sh'
FAKE = r'''
import json,os,pathlib,signal,sys,time
root=pathlib.Path(os.environ['TEST_ROOT'])
args=sys.argv[1:]
tool=pathlib.Path(sys.argv[0]).name
if tool=='curl': raise SystemExit(int(os.environ.get('CURL_EXIT','0')))
if tool=='sudo': os.execvp('docker',['docker',*args[2:]])
if args[0]=='info': raise SystemExit(int(os.environ.get('INFO_EXIT','0')))
if args[0]=='stop':
 (root/'stopped-name').write_text(args[-1])
 raise SystemExit(0)
if args[0]!='run': raise SystemExit(99)
files=[pathlib.Path(args[i+1]) for i,v in enumerate(args) if v=='--env-file']
(root/'run.json').write_text(json.dumps({'args':args,'files':[str(p) for p in files],
 'modes':[p.stat().st_mode & 0o777 for p in files]}))
def stop(sig,frame):
 (root/'caught').write_text(str(sig))
 raise SystemExit(0)
for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,stop)
(root/'ready').write_text(str(os.getpid()))
if os.environ.get('WAIT'):time.sleep(30)
raise SystemExit(int(os.environ.get('RUN_EXIT','0')))
'''


class HostNetworkLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='map1-host-tool-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'fake'
        fake.write_text(f'#!{sys.executable}\n' + FAKE)
        fake.chmod(0o700)
        for tool in ('docker', 'curl', 'sudo'):
            (self.bin / tool).symlink_to(fake)
        self.tmp = self.root / 'temporary'
        self.tmp.mkdir()
        self.runtime = self.root / 'runtime.env'
        self.runtime.write_text('POSTGRES_DB=fixture\nPOSTGRES_APP_USER=fixture_app\n'
                                'POSTGRES_APP_PASSWORD=fixture-private-value\n')
        self.env = dict(PATH=f'{self.bin}:/usr/bin:/bin', TEST_ROOT=str(self.root), TMPDIR=str(self.tmp),
                        MAP1_ENV_FILE=str(self.runtime), MAP1_STAGE3_REPORT_DIR=str(self.root / 'reports'),
                        MAP1_ALLOW_DIRECT_EGRESS='1', MAP1_OGM_PROXY='http://<USER>:<PASSWORD>@localhost:9')

    def run_tool(self, **changes):
        return subprocess.run(['bash', str(SCRIPT), 'fixture-command'], env=dict(self.env, **changes),
                              capture_output=True, text=True, timeout=10)

    def assert_clean(self):
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_success_failure_sudo_and_missing_env_preserve_status_and_remove_secrets(self):
        for changes in ({}, {'RUN_EXIT': '7'}, {'INFO_EXIT': '1'},
                        {'MAP1_ENV_FILE': str(self.root / 'missing')}):
            with self.subTest(changes=changes):
                result = self.run_tool(**changes)
                self.assertEqual(result.returncode, int(changes.get('RUN_EXIT', '0')), result.stderr)
                data = json.loads((self.root / 'run.json').read_text())
                self.assertTrue(data['files'])
                self.assertTrue(all(mode == 0o600 for mode in data['modes']))
                self.assertTrue(all(not Path(p).exists() for p in data['files']))
                self.assertNotIn(self.env['MAP1_OGM_PROXY'], ' '.join(data['args']))
                self.assertNotIn('fixture-private-value', result.stdout + result.stderr)
                self.assert_clean()

    def test_failed_preflight_removes_secrets_without_starting_container(self):
        result = self.run_tool(CURL_EXIT='22')
        self.assertEqual(result.returncode, 22)
        self.assertFalse((self.root / 'run.json').exists())
        self.assert_clean()

    def test_wrapper_signals_forward_and_clean_only_owned_container(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                for name in ('ready', 'caught', 'stopped-name'):
                    (self.root / name).unlink(missing_ok=True)
                process = subprocess.Popen(['bash', str(SCRIPT), 'fixture-command'],
                    env=dict(self.env, WAIT='1'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True)
                try:
                    deadline = time.monotonic() + 5
                    while not (self.root / 'ready').exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue((self.root / 'ready').exists())
                    process.send_signal(sig)
                    _, errors = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 128 + sig, errors)
                    self.assertEqual((self.root / 'caught').read_text(), str(sig))
                    args = json.loads((self.root / 'run.json').read_text())['args']
                    name = args[args.index('--name') + 1]
                    self.assertEqual((self.root / 'stopped-name').read_text(), name)
                    self.assert_clean()
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
