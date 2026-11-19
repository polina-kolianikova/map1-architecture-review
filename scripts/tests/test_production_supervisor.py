"""Offline supervisor safety review: all host and Docker operations are mocked.

Safety expectations intentionally fail where the reviewed launcher lacks a guard.
Run with python3 -B -m unittest discover -s scripts/tests
    -p test_production_supervisor.py -v
"""
import copy
from contextlib import ExitStack, contextmanager
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    'production_supervisor', Path(__file__).resolve().parents[1] / 'run-production-services.py'
)
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)

APP_SERVICES = ('db', 'redis', 'backend', 'frontend', 'caddy')


def healthy_containers():
    return [
        {
            'Id': format(index, '064x'),
            'Name': '/map1-' + service + '-1',
            'Config': {'Labels': {
                'com.docker.compose.project': 'map1',
                'com.docker.compose.service': service,
            }},
            'HostConfig': {'RestartPolicy': {'Name': 'no'}},
            'Mounts': [
                {'Type': 'bind', 'RW': True, 'Destination': '/var/lib/postgresql/data',
                 'Source': str(supervisor.STORAGE / 'postgres')},
                {'Type': 'bind', 'RW': True, 'Destination': '/var/lib/postgresql/stage4_tablespace',
                 'Source': str(supervisor.STORAGE / 'tablespace')},
            ] if service == 'db' else [],
            'State': {'Running': True, 'Health': {'Status': 'healthy'}},
        }
        for index, service in enumerate(APP_SERVICES, 1)
    ]


def resolved_model():
    budgets = {'db': (6, 16 * 1024**3, 256), 'redis': (.25, 256 * 1024**2, 64),
               'backend': (1, 1024**3, 128), 'frontend': (.5, 512 * 1024**2, 64),
               'caddy': (.25, 256 * 1024**2, 64)}
    services = {name: {'restart': 'no', 'cpus': cpu, 'mem_limit': memory,
                       'memswap_limit': memory, 'pids_limit': pids, 'environment': {}}
                for name, (cpu, memory, pids) in budgets.items()}
    services['db']['volumes'] = [
        {'type': 'bind', 'source': str(supervisor.STORAGE / name), 'target': target,
         'bind': {'create_host_path': False}}
        for target, name in (('/var/lib/postgresql/data', 'postgres'),
                             ('/var/lib/postgresql/stage4_tablespace', 'tablespace'))
    ]
    services['backend'].update(image='sha256:' + '1' * 64,
        command=['uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000', '--workers', '2'])
    services['backend']['environment']['STAGE4_SCHEDULER_ENABLED'] = 'false'
    services['frontend']['image'] = 'sha256:' + '2' * 64
    for name in ('stage4-worker', 'stage4-beat'):
        services[name] = {'profiles': ['processing']}
    return {'services': services}


class ProductionSupervisorTest(unittest.TestCase):
    def setUp(self):
        # Any operation accidentally escaping the command mocks must fail locally.
        for name in ('run', 'Popen'):
            guard = mock.patch.object(
                supervisor.subprocess, name,
                side_effect=AssertionError('A test attempted a real subprocess'),
            )
            guard.start()
            self.addCleanup(guard.stop)
        closing = mock.patch.object(supervisor, 'CLOSING', False)
        closing.start()
        self.addCleanup(closing.stop)

    @contextmanager
    def configuration(self, model, override=None):
        if override is None:
            baseline = resolved_model()
            override = {'services': {s: {'image': baseline['services'][s]['image']}
                                     for s in ('backend', 'frontend')}}
        app = mock.MagicMock()
        app.__truediv__.return_value.read_text.return_value = json.dumps(override)
        with mock.patch.object(supervisor, 'APP', app), mock.patch.object(
                supervisor, 'command', return_value=json.dumps(model)):
            yield

    @contextmanager
    def inspection(self, values):
        # Inventory has separate end-to-end negative tests below.
        with mock.patch.object(supervisor, 'check_inventory'), mock.patch.object(
                supervisor, 'command', return_value=json.dumps(values)):
            yield

    @contextmanager
    def launcher(self, *, storage_failure=None, command_failure=None, cancel_before_up=False):
        handlers = {}

        def install_signal(signum, handler):
            handlers[signum] = handler
            if cancel_before_up and signum == supervisor.signal.SIGINT:
                handler(signum, None)

        def finish_after_inspection():
            handlers[supervisor.signal.SIGTERM](supervisor.signal.SIGTERM, None)
            return healthy_containers()

        def command(args, **kwargs):
            action = args[len(supervisor.COMPOSE)]
            if action == command_failure:
                raise RuntimeError('Synthetic ' + action + ' failure')
            return ''

        with ExitStack() as stack:
            stack.enter_context(mock.patch('sys.argv', ['run-production-services.py', 'run']))
            stack.enter_context(mock.patch('builtins.open', mock.mock_open()))
            stack.enter_context(mock.patch.object(supervisor.socket, 'gethostname', return_value='<LOCAL_HOSTNAME>'))
            lock = stack.enter_context(mock.patch.object(supervisor.fcntl, 'flock'))
            stack.enter_context(mock.patch.object(supervisor.signal, 'signal', side_effect=install_signal))
            stack.enter_context(mock.patch.object(supervisor.time, 'sleep', side_effect=AssertionError('Unexpected sleep')))
            storage = stack.enter_context(mock.patch.object(supervisor, 'check_storage', side_effect=storage_failure))
            validate = stack.enter_context(mock.patch.object(supervisor, 'validate_configuration',
                side_effect=lambda: supervisor.command(supervisor.COMPOSE + ['config', '--format', 'json'])))
            inventory = stack.enter_context(mock.patch.object(supervisor, 'check_inventory'))
            inspect = stack.enter_context(mock.patch.object(supervisor, 'inspect_services', side_effect=finish_after_inspection))
            commands = stack.enter_context(mock.patch.object(supervisor, 'command', side_effect=command))
            yield SimpleNamespace(commands=commands, storage=storage, inspect=inspect, lock=lock,
                                  validate=validate, inventory=inventory)

    def actions(self, commands):
        return [call.args[0][len(supervisor.COMPOSE):] for call in commands.call_args_list]

    def test_start_and_stop_use_only_five_application_services(self):
        with self.launcher() as calls:
            supervisor.main()
        actions = self.actions(calls.commands)
        self.assertEqual([a[0] for a in actions], ['config', 'up', 'stop'])
        start, stop = actions[1:]
        self.assertEqual(tuple(start[-5:]), APP_SERVICES)
        self.assertEqual(tuple(stop[-5:]), APP_SERVICES)
        self.assertIn('--no-deps', start)
        self.assertIn('--no-build', start)
        self.assertIn('--wait', start)
        self.assertTrue(set(start).isdisjoint({'stage4-worker', 'stage4-beat', 'stage1-tools', 'migrate'}))
        calls.validate.assert_called_once_with()
        calls.inventory.assert_called_once_with()

    def test_initial_mount_failure_prevents_any_compose_command(self):
        with self.launcher(storage_failure=RuntimeError('Synthetic mount refusal')) as calls:
            with self.assertRaisesRegex(RuntimeError, 'mount refusal'):
                supervisor.main()
        calls.commands.assert_not_called()

    def test_invalid_configuration_prevents_start(self):
        with self.launcher(command_failure='config') as calls:
            with self.assertRaisesRegex(RuntimeError, 'config failure'):
                supervisor.main()
        self.assertEqual([a[0] for a in self.actions(calls.commands)], ['config'])

    def test_second_supervisor_cannot_start_services(self):
        with self.launcher() as calls:
            calls.lock.side_effect = BlockingIOError('Synthetic lock contention')
            with self.assertRaises(BlockingIOError):
                supervisor.main()
        calls.storage.assert_not_called()
        calls.commands.assert_not_called()

    def test_partial_start_failure_stops_application_services(self):
        with self.launcher(command_failure='up') as calls:
            with self.assertRaisesRegex(RuntimeError, 'up failure'):
                supervisor.main()
        self.assertEqual([a[0] for a in self.actions(calls.commands)], ['config', 'up', 'stop'])

    def test_mount_failure_after_start_stops_application_services(self):
        with self.launcher(storage_failure=[None, RuntimeError('Synthetic mount loss')]) as calls:
            with self.assertRaisesRegex(RuntimeError, 'mount loss'):
                supervisor.main()
        self.assertEqual(self.actions(calls.commands)[-1][0], 'stop')
        calls.inspect.assert_not_called()

    def test_unhealthy_application_stops_services(self):
        with self.launcher() as calls:
            calls.inspect.side_effect = RuntimeError('Synthetic unhealthy application')
            with self.assertRaisesRegex(RuntimeError, 'unhealthy application'):
                supervisor.main()
        self.assertEqual(self.actions(calls.commands)[-1][0], 'stop')

    def test_termination_before_up_must_not_start_services(self):
        with self.launcher(cancel_before_up=True) as calls:
            supervisor.main()
        self.assertNotIn('up', [a[0] for a in self.actions(calls.commands)],
                         'A termination request must cancel a not-yet-started deployment')

    def test_all_healthy_application_services_are_accepted(self):
        values = healthy_containers()
        with self.inspection(values):
            self.assertEqual(supervisor.inspect_services(), values)

    def test_wrong_identity_mount_and_health_are_rejected(self):
        cases = []
        values = healthy_containers()
        values[0]['Config']['Labels']['com.docker.compose.project'] = 'other-project'
        cases.append(values)
        values = healthy_containers()
        values[1]['Config']['Labels']['com.docker.compose.service'] = 'stage4-worker'
        cases.append(values)
        values = healthy_containers()
        values[0]['Mounts'][0]['Source'] = '/unverified/postgres'
        cases.append(values)
        values = healthy_containers()
        values[2]['State']['Running'] = False
        cases.append(values)
        values = healthy_containers()
        values[3]['State']['Health']['Status'] = 'unhealthy'
        cases.append(values)
        values = healthy_containers()
        del values[4]['State']['Health']
        cases.append(values)
        for index, values in enumerate(cases):
            with self.subTest(case=index), self.inspection(values):
                with self.assertRaises(RuntimeError):
                    supervisor.inspect_services()

    def test_incomplete_inspection_is_rejected(self):
        for values in ([], healthy_containers()[:-1]):
            with self.subTest(count=len(values)), self.inspection(values):
                with self.assertRaises(RuntimeError, msg='Missing service evidence must fail closed'):
                    supervisor.inspect_services()

    def test_docker_autorestart_policy_is_rejected(self):
        values = healthy_containers()
        values[0]['HostConfig']['RestartPolicy']['Name'] = 'unless-stopped'
        with self.inspection(values):
            with self.assertRaises(RuntimeError, msg='Docker restart must not bypass the UUID gate'):
                supervisor.inspect_services()

    def test_running_worker_is_rejected_even_when_application_is_healthy(self):
        apps = healthy_containers()
        worker = copy.deepcopy(apps[0])
        worker['Id'] = 'f' * 64
        worker['Name'] = '/map1-stage4-worker-1'
        worker['Config']['Labels']['com.docker.compose.service'] = 'stage4-worker'

        def docker_reply(args, **kwargs):
            if args[:2] == ['docker', 'ps']:
                return worker['Id']
            if args[:2] == ['docker', 'inspect']:
                if worker['Id'] in args or worker['Name'].lstrip('/') in args:
                    return json.dumps([worker])
                return json.dumps(apps)
            raise AssertionError('Unexpected non-inspection command')

        app = mock.MagicMock()
        app.__truediv__.return_value.is_file.return_value = False
        with mock.patch.object(supervisor, 'APP', app), mock.patch.object(supervisor, 'command', side_effect=docker_reply):
            with self.assertRaises(RuntimeError, msg='A live worker violates application-only operation'):
                supervisor.inspect_services()

    def test_stop_survives_unreadable_compose_configuration(self):
        values = healthy_containers()
        stopped = []

        def reply(args, **kwargs):
            if args[:len(supervisor.COMPOSE)] == supervisor.COMPOSE:
                raise RuntimeError('Synthetic configuration unavailable')
            if args[:2] == ['docker', 'ps']:
                return '\n'.join(c['Id'] for c in values if c['State']['Running'])
            if args[:2] == ['docker', 'inspect']:
                return json.dumps([c for c in values if c['Id'] in args or c['Name'].lstrip('/') in args])
            if args[:2] == ['docker', 'stop']:
                stopped.extend(args[2:])
                for value in values:
                    if value['Id'] in args:
                        value['State']['Running'] = False
                return ''
            raise AssertionError('Unexpected recovery command')

        with mock.patch.object(supervisor, 'command', side_effect=reply):
            try:
                supervisor.stop()
            except RuntimeError:
                self.fail('Shutdown abandoned live containers when Compose configuration became unavailable')
        for value in values:
            self.assertTrue(value['Id'] in stopped or value['Name'].lstrip('/') in stopped)
        self.assertFalse(any('worker' in item or 'beat' in item for item in stopped))

    def test_pg_version_symlink_and_device_checks(self):
        for invalid in (None, 'version', 'postgres-symlink', 'tablespace-symlink', 'device'):
            storage = mock.MagicMock()
            pg = mock.MagicMock()
            tablespace = mock.MagicMock()
            storage.__truediv__.side_effect = {'postgres': pg, 'tablespace': tablespace}.__getitem__
            storage.stat.return_value.st_dev = 11
            for path in (pg, tablespace):
                path.is_symlink.return_value = False
                path.is_dir.return_value = True
                path.stat.return_value.st_dev = 11
            pg.__truediv__.return_value.read_text.return_value = '16\n'
            if invalid == 'version':
                pg.__truediv__.return_value.read_text.return_value = '15\n'
            elif invalid == 'postgres-symlink':
                pg.is_symlink.return_value = True
            elif invalid == 'tablespace-symlink':
                tablespace.is_symlink.return_value = True
            elif invalid == 'device':
                pg.stat.return_value.st_dev = 22
            with self.subTest(invalid=invalid), mock.patch.object(supervisor, 'STORAGE', storage), mock.patch.object(supervisor, 'command') as command:
                if invalid is None:
                    supervisor.check_storage()
                else:
                    with self.assertRaises(RuntimeError):
                        supervisor.check_storage()
                self.assertIn('--mount-only', command.call_args.args[0])
                self.assertIn(supervisor.UUID, command.call_args.args[0])

    def test_mount_gate_failure_does_not_inspect_pgdata(self):
        with mock.patch.object(supervisor, 'command', side_effect=RuntimeError('Synthetic UUID refusal')), mock.patch.object(supervisor, 'STORAGE') as storage:
            with self.assertRaisesRegex(RuntimeError, 'UUID refusal'):
                supervisor.check_storage()
        storage.__truediv__.assert_not_called()

    def test_inventory_refusal_prevents_start(self):
        with self.launcher() as calls:
            calls.inventory.side_effect = RuntimeError('Synthetic unauthorized worker')
            with self.assertRaisesRegex(RuntimeError, 'unauthorized worker'):
                supervisor.main()
        self.assertNotIn('up', [a[0] for a in self.actions(calls.commands)])

    def test_valid_resolved_configuration_is_accepted(self):
        with self.configuration(resolved_model()):
            supervisor.validate_configuration()

    def test_configuration_rejects_restart_and_unbounded_resources(self):
        for service in APP_SERVICES:
            for field, value in (('restart', 'unless-stopped'), ('cpus', 0),
                                 ('mem_limit', 0), ('pids_limit', -1)):
                model = resolved_model()
                model['services'][service][field] = value
                with self.subTest(service=service, field=field), self.configuration(model):
                    with self.assertRaises(RuntimeError):
                        supervisor.validate_configuration()

    def test_configuration_rejects_unsafe_database_mounts(self):
        for index in (0, 1):
            for field, value in (('source', '/unverified/data'), ('type', 'volume'),
                                 ('bind', {'create_host_path': True}), ('bind', {})):
                model = resolved_model()
                model['services']['db']['volumes'][index][field] = value
                with self.subTest(mount=index, field=field, value=value), self.configuration(model):
                    with self.assertRaises(RuntimeError):
                        supervisor.validate_configuration()

    def test_configuration_rejects_wrong_release_scheduler_and_profiles(self):
        models = []
        for service in ('backend', 'frontend'):
            model = resolved_model()
            model['services'][service]['image'] = 'sha256:' + 'f' * 64
            models.append(model)
        model = resolved_model()
        model['services']['backend']['environment']['STAGE4_SCHEDULER_ENABLED'] = 'true'
        models.append(model)
        for service in ('stage4-worker', 'stage4-beat'):
            model = resolved_model()
            model['services'][service]['profiles'] = []
            models.append(model)
        for index, model in enumerate(models):
            with self.subTest(case=index), self.configuration(model):
                with self.assertRaises(RuntimeError):
                    supervisor.validate_configuration()

    def test_configuration_rejects_pgdata_environment_redirection(self):
        model = resolved_model()
        model['services']['db']['environment']['PGDATA'] = '/tmp/unverified-cluster'
        with self.configuration(model):
            with self.assertRaises(RuntimeError, msg='PostgreSQL must actually use the verified cluster mount'):
                supervisor.validate_configuration()

    def test_configuration_rejects_processing_command_under_backend_name(self):
        model = resolved_model()
        model['services']['backend']['command'] = ['python', '-m', 'app.commands.run_stage1_pipeline']
        with self.configuration(model):
            with self.assertRaises(RuntimeError, msg='Application service names must not conceal processing commands'):
                supervisor.validate_configuration()

    def test_cancelled_start_interrupts_and_reaps_child(self):
        args = supervisor.COMPOSE + ['up', '--detach', '--no-deps', *APP_SERVICES]
        process = mock.Mock(returncode=0)
        process.poll.return_value = None
        process.communicate.side_effect = [supervisor.subprocess.TimeoutExpired(args, 1), ('', '')]
        with mock.patch.object(supervisor, 'CLOSING', True), mock.patch.object(
                supervisor.subprocess, 'Popen', return_value=process), mock.patch.object(
                supervisor.time, 'monotonic', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                supervisor.command(args)
        process.send_signal.assert_called_once_with(supervisor.signal.SIGINT)
        process.kill.assert_not_called()
        self.assertEqual(process.communicate.call_args_list,
                         [mock.call(timeout=1), mock.call(timeout=15)])

    def test_unresponsive_cancelled_start_is_killed_and_reaped(self):
        args = supervisor.COMPOSE + ['up', '--detach', *APP_SERVICES]
        process = mock.Mock(returncode=0)
        process.poll.return_value = None
        process.communicate.side_effect = [supervisor.subprocess.TimeoutExpired(args, 1),
                                          supervisor.subprocess.TimeoutExpired(args, 15), ('', '')]
        with mock.patch.object(supervisor, 'CLOSING', True), mock.patch.object(
                supervisor.subprocess, 'Popen', return_value=process), mock.patch.object(
                supervisor.time, 'monotonic', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                supervisor.command(args)
        process.send_signal.assert_called_once_with(supervisor.signal.SIGINT)
        process.kill.assert_called_once_with()
        self.assertEqual(process.communicate.call_args_list,
                         [mock.call(timeout=1), mock.call(timeout=15), mock.call(timeout=5)])

    def test_shutdown_command_is_not_cancelled_by_closing_flag(self):
        args = supervisor.COMPOSE + ['stop', '--timeout', '60', *APP_SERVICES]
        process = mock.Mock(returncode=0)
        process.communicate.side_effect = [supervisor.subprocess.TimeoutExpired(args, 1), ('stopped', '')]
        with mock.patch.object(supervisor, 'CLOSING', True), mock.patch.object(
                supervisor.subprocess, 'Popen', return_value=process), mock.patch.object(
                supervisor.time, 'monotonic', return_value=0):
            self.assertEqual(supervisor.command(args), 'stopped')
        process.send_signal.assert_not_called()
        process.kill.assert_not_called()

    def test_command_deadline_interrupts_and_reaps_child(self):
        args = ['docker', 'inspect', 'fixture-container']
        process = mock.Mock(returncode=0)
        process.poll.return_value = None
        process.communicate.side_effect = [supervisor.subprocess.TimeoutExpired(args, 1), ('', '')]
        with mock.patch.object(supervisor.subprocess, 'Popen', return_value=process), mock.patch.object(
                supervisor.time, 'monotonic', side_effect=[0, 11]):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                supervisor.command(args, timeout=10)
        process.send_signal.assert_called_once_with(supervisor.signal.SIGINT)
        self.assertEqual(process.communicate.call_args_list,
                         [mock.call(timeout=1), mock.call(timeout=15)])

    def test_command_failure_does_not_expose_captured_output(self):
        process = mock.Mock(returncode=1)
        process.communicate.return_value = ('synthetic-private-output', 'synthetic-private-error')
        with mock.patch.object(supervisor.subprocess, 'Popen', return_value=process):
            with self.assertRaises(RuntimeError) as raised:
                supervisor.command(['docker', 'compose', 'config'])
        self.assertNotIn('synthetic-private', str(raised.exception))

    def test_stop_fallback_rejects_wrong_project_targets(self):
        values = healthy_containers()
        values[0]['Config']['Labels']['com.docker.compose.project'] = 'isolated-rehearsal'
        with mock.patch.object(supervisor, 'command', side_effect=[
                RuntimeError('Synthetic configuration unavailable'),
                '\n'.join(c['Id'] for c in values), json.dumps(values)]) as command:
            with self.assertRaisesRegex(RuntimeError, 'safe shutdown'):
                supervisor.stop()
        self.assertFalse(any(c.args[0][:2] == ['docker', 'stop'] for c in command.call_args_list))

    def test_stop_fallback_handles_partial_deployment(self):
        values = healthy_containers()[:3]
        by_identifier = {key: c for c in values for key in (c['Id'], c['Name'].lstrip('/'))}
        stopped = []

        def reply(args, **kwargs):
            if args[:len(supervisor.COMPOSE)] == supervisor.COMPOSE:
                raise RuntimeError('Synthetic configuration unavailable')
            if args[:2] == ['docker', 'ps']:
                return '\n'.join(c['Id'] for c in values)
            if args[:2] == ['docker', 'inspect']:
                requested = args[2:]
                if any(name not in by_identifier for name in requested):
                    raise RuntimeError('Synthetic inspect failure: service was never created')
                return json.dumps([by_identifier[name] for name in requested])
            if args[:2] == ['docker', 'stop']:
                stopped.extend(args[2:])
                for value in values:
                    if value['Id'] in args:
                        value['State']['Running'] = False
                return ''
            raise AssertionError('Unexpected recovery command')

        with mock.patch.object(supervisor, 'command', side_effect=reply):
            try:
                supervisor.stop()
            except RuntimeError:
                self.fail('Shutdown must still stop verified existing containers after partial startup')
        for value in values:
            self.assertTrue(value['Id'] in stopped or value['Name'].lstrip('/') in stopped)

    def test_shutdown_fallback_fits_systemd_stop_budget(self):
        unit = (Path(__file__).resolve().parents[2] / 'infra/systemd/map1-production.service').read_text()
        unit_budget = int(re.search(r'^TimeoutStopSec=(\d+)$', unit, re.MULTILINE).group(1))
        elapsed = 0
        values = healthy_containers()

        def delayed_reply(args, timeout=60):
            nonlocal elapsed
            if args[:len(supervisor.COMPOSE)] == supervisor.COMPOSE:
                elapsed += timeout
                raise RuntimeError('Synthetic Compose stop timeout')
            elapsed += timeout - 1
            if args[:2] == ['docker', 'ps']:
                return '\n'.join(c['Id'] for c in values if c['State']['Running'])
            if args[:2] == ['docker', 'inspect']:
                return json.dumps([c for c in values if c['Id'] in args])
            if args[:2] == ['docker', 'stop']:
                for value in values:
                    if value['Id'] in args:
                        value['State']['Running'] = False
                return ''
            raise AssertionError('Unexpected recovery command')

        with mock.patch.object(supervisor, 'command', side_effect=delayed_reply):
            supervisor.stop()
        self.assertLess(elapsed, unit_budget,
                        'Permitted cleanup time must leave room before systemd kills the supervisor')

    def test_stop_fallback_rejects_containers_still_running_after_stop(self):
        values = healthy_containers()
        with mock.patch.object(supervisor, 'command', side_effect=[
                RuntimeError('Synthetic configuration unavailable'),
                '\n'.join(c['Id'] for c in values), json.dumps(values), '', json.dumps(values)]):
            with self.assertRaisesRegex(RuntimeError, 'shutdown was not confirmed'):
                supervisor.stop()


if __name__ == '__main__':
    unittest.main()
