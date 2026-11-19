"""Negative capacity/mount fixtures. No Docker, systemd or server calls."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('storage_gate', Path(__file__).resolve().parents[1] / 'check-production-storage.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class ProductionStorageTest(unittest.TestCase):
    def setUp(self):
        self.uuid = '00000000-0000-4000-8000-000000000001'
        self.fixture = {
            'hostname': 'fixture-host', 'is_mount': True, 'canonical_mount': True,
            'mount': {'target': '/srv/map1/storage', 'uuid': self.uuid, 'fstype': 'ext4', 'options': 'rw,noatime'},
            'marker_uuid': self.uuid, 'marker_private': True, 'storage_paths_valid': True,
            'storage': {'free_bytes': 100 * gate.GIB, 'free_inodes': 100001},
            'root': {'free_bytes': 40 * gate.GIB, 'free_inodes': 100000},
            'cgroup': {'memory_max': 8 * gate.GIB, 'swap_max': 0, 'cpu_quota': 400000,
                       'cpu_period': 100000, 'pids_max': 256},
            'containers': [self.container('db'), self.container('stage4-worker')],
            'memory_total': 32 * gate.GIB, 'memory_available': 16 * gate.GIB,
        }

    def container(self, service):
        return {'service': service, 'parent': gate.SLICE, 'actual_parent_matches': True,
                'memory': 3 * gate.GIB, 'cpus': 2, 'pids': 128}

    def check(self, fixture=None, **options):
        return gate.evaluate_snapshot(fixture or self.fixture, expected_uuid=self.uuid,
                                       expected_hostname='fixture-host', **options)

    def test_valid_exact_reserves_and_requested_worker(self):
        self.assertEqual(self.check(requested_workers=1), [])

    def test_absent_wrong_readonly_or_wrong_type_mount_rejected(self):
        for field, value in [('target', '/'), ('uuid', 'wrong'), ('fstype', 'tmpfs'), ('options', 'ro,noatime')]:
            f = copy.deepcopy(self.fixture)
            f['mount'][field] = value
            self.assertTrue(self.check(f), field)

    def test_wrong_host_marker_symlinks_and_missing_paths_rejected(self):
        for key, value in [('hostname', 'other'), ('marker_uuid', 'other'), ('is_mount', False),
                           ('canonical_mount', False), ('marker_private', False), ('storage_paths_valid', False)]:
            f = copy.deepcopy(self.fixture)
            f[key] = value
            self.assertTrue(self.check(f), key)

    def test_low_root_or_storage_bytes_and_inodes_rejected(self):
        for directory in ('root', 'storage'):
            for field in ('free_bytes', 'free_inodes'):
                f = copy.deepcopy(self.fixture)
                f[directory][field] = 0
                self.assertTrue(self.check(f), (directory, field))

    def test_missing_or_excessive_kernel_limits_rejected(self):
        for key, value in [('memory_max', 0), ('memory_max', 9 * gate.GIB), ('swap_max', gate.GIB),
                           ('cpu_quota', 500000), ('cpu_period', 0), ('pids_max', 300)]:
            f = copy.deepcopy(self.fixture)
            f['cgroup'][key] = value
            self.assertTrue(self.check(f), key)

    def test_uncapped_or_escaped_worker_rejected(self):
        for key, value in [('memory', 0), ('cpus', 0), ('pids', -1),
                           ('parent', ''), ('actual_parent_matches', False)]:
            f = copy.deepcopy(self.fixture)
            f['containers'][1][key] = value
            self.assertTrue(self.check(f), key)

    def test_worker_admission_and_aggregate_memory_rejected(self):
        self.assertTrue(self.check(requested_workers=2))
        for key, value in [('memory_total', 10 * gate.GIB), ('memory_available', gate.GIB)]:
            f = copy.deepcopy(self.fixture)
            f[key] = value
            self.assertTrue(self.check(f), key)

    def test_mount_only_does_not_require_docker_or_capacity_fields(self):
        f = copy.deepcopy(self.fixture)
        for key in ('cgroup', 'containers', 'memory_total', 'memory_available'):
            del f[key]
        self.assertEqual(gate.evaluate_storage(f, expected_uuid=self.uuid, expected_hostname='fixture-host'), [])


if __name__ == '__main__':
    unittest.main()
