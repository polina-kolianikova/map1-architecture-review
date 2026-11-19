#!/usr/bin/env python3
"""Read-only preflight: UUID, storage/root reserves and aggregate worker capacity.

Does not mount, create paths, start jobs, migrate PostgreSQL or authorize a country
run. evaluate_snapshot() accepts synthetic evidence for offline negative tests;
the CLI always collects current host evidence and has no fixture/bypass option.
"""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

GIB = 1024**3
WORKER_SERVICES = {'stage4-worker', 'stage4-beat', 'stage1-tools', 'migrate'}
SLICE = 'map1-workers.slice'


def evaluate_storage(s, *, expected_uuid, expected_hostname, min_storage_gib=40,
                     min_root_gib=40, min_inodes=100000):
    """Return failures, never silently accepting incomplete evidence."""
    errors = []
    def require(condition, message):
        if not condition:
            errors.append(message)
    require(s['hostname'] == expected_hostname, 'Unexpected host')
    m = s['mount']
    require(s['is_mount'] and s['canonical_mount'], 'Storage must be the exact real mountpoint')
    require(m['target'] == '/srv/map1/storage' and m['uuid'] == expected_uuid
            and m['fstype'] == 'ext4' and 'rw' in m['options'].split(','),
            'Storage UUID/target/type/writable mount mismatch')
    require(s['marker_uuid'] == expected_uuid and s['marker_private'], 'Volume identity marker mismatch or unsafe permissions')
    require(s['storage_paths_valid'], 'Processing paths must already exist on the verified storage filesystem')
    for key, floor in [('storage', min_storage_gib), ('root', min_root_gib)]:
        require(s[key]['free_bytes'] >= floor * GIB and s[key]['free_inodes'] >= min_inodes,
                key + ' filesystem reserve breached')
    return errors


def evaluate_snapshot(s, *, expected_uuid, expected_hostname, min_storage_gib=40,
                      min_root_gib=40, min_inodes=100000, max_workers=2,
                      requested_workers=0, host_reserve_gib=2):
    errors = evaluate_storage(s, expected_uuid=expected_uuid, expected_hostname=expected_hostname,
                              min_storage_gib=min_storage_gib, min_root_gib=min_root_gib,
                              min_inodes=min_inodes)
    def require(condition, message):
        if not condition:
            errors.append(message)
    # Check actual cgroup-v2 limits, not just the presence of a unit file. More
    # containers cannot escape this aggregate RAM/CPU/PID ceiling by scaling.
    c = s['cgroup']
    require(0 < c['memory_max'] <= 8 * GIB and c['swap_max'] == 0
            and c['cpu_period'] > 0 and 0 < c['cpu_quota'] / c['cpu_period'] <= 4
            and 0 < c['pids_max'] <= 256, 'Aggregate worker slice is not bounded as specified')
    workers = [x for x in s['containers'] if x['service'] in WORKER_SERVICES]
    jobs = [x for x in workers if x['service'] != 'stage4-beat']
    require(len(jobs) + requested_workers <= max_workers, 'Aggregate worker-count admission limit exceeded')
    for container in s['containers']:
        require(container['memory'] > 0 and container['cpus'] > 0 and container['pids'] > 0,
                'A project container has unlimited RAM/CPU/PIDs')
    for worker in workers:
        require(worker['parent'] == SLICE and worker['actual_parent_matches'],
                'Worker is outside the bounded aggregate slice')
    other_memory = sum(x['memory'] for x in s['containers'] if x['service'] not in WORKER_SERVICES)
    require(other_memory + c['memory_max'] + host_reserve_gib * GIB <= s['memory_total'],
            'Project ceilings plus OS reserve exceed host RAM')
    require(s['memory_available'] >= (requested_workers * 3 + host_reserve_gib) * GIB,
            'Insufficient available RAM for requested workers and OS reserve')
    return errors


def command(args):
    return subprocess.check_output(args, text=True, timeout=15).strip()


def filesystem(path):
    v = os.statvfs(path)
    return {'free_bytes': v.f_bavail * v.f_frsize, 'free_inodes': v.f_favail}


def collect_storage(storage_root):
    mount = Path('/srv/map1/storage')
    found = json.loads(command(['findmnt', '--json', '-o', 'TARGET,UUID,FSTYPE,OPTIONS',
                                '-T', str(mount)]))['filesystems'][0]
    marker = mount / '.map1-volume.json'
    marker_stat = marker.lstat()
    marker_uuid = json.loads(marker.read_text())['uuid']
    paths_valid = storage_root.is_absolute() and storage_root.resolve().is_relative_to(mount)
    for name in ('artifacts', 'cache', 'zonal', 'work'):
        path = storage_root / name
        paths_valid = paths_valid and path.is_dir() and path.resolve().is_relative_to(mount)
        paths_valid = paths_valid and path.stat().st_dev == mount.stat().st_dev
    return {'hostname': socket.gethostname(), 'mount': found, 'is_mount': os.path.ismount(mount),
            'canonical_mount': mount.resolve() == mount, 'marker_uuid': marker_uuid,
            'marker_private': not marker.is_symlink() and marker_stat.st_uid == 0 and marker_stat.st_mode & 0o077 == 0,
            'storage_paths_valid': paths_valid, 'storage': filesystem(mount), 'root': filesystem('/')}


def collect_snapshot(project, storage_root):
    storage = collect_storage(storage_root)
    control = command(['systemctl', 'show', SLICE, '--property=ControlGroup', '--value'])
    if not control.startswith('/') or '..' in Path(control).parts:
        raise ValueError('Invalid worker control group')
    cg = Path('/sys/fs/cgroup') / control.lstrip('/')
    # int('max') intentionally fails closed for missing/unlimited cgroup limits.
    quota, period = map(int, (cg / 'cpu.max').read_text().split())
    cgroup = {'memory_max': int((cg / 'memory.max').read_text()),
              'swap_max': int((cg / 'memory.swap.max').read_text()),
              'pids_max': int((cg / 'pids.max').read_text()), 'cpu_quota': quota, 'cpu_period': period}
    ids = command(['docker', 'ps', '-q', '--no-trunc', '--filter', 'label=com.docker.compose.project=' + project]).split()
    containers = []
    for cid in ids:
        # Deliberately never retrieve environment variables or credentials.
        raw = command(['docker', 'inspect', '--format',
            '{{index .Config.Labels "com.docker.compose.service"}}|{{.HostConfig.CgroupParent}}|{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.PidsLimit}}|{{.State.Pid}}', cid])
        service, parent, memory, cpus, pids, pid = raw.split('|')
        actual = Path('/proc') / pid / 'cgroup'
        groups = [line.split(':', 2)[2] for line in actual.read_text().splitlines() if line.startswith('0::')]
        containers.append({'service': service, 'parent': parent, 'memory': int(memory),
            'cpus': int(cpus) / 10**9, 'pids': int(pids),
            'actual_parent_matches': any(g.startswith(control.rstrip('/') + '/') for g in groups)})
    mem = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in ('MemTotal', 'MemAvailable'):
            mem[key] = int(value.split()[0]) * 1024
    if not containers:
        raise ValueError('No active project containers; cannot establish aggregate capacity')
    return {**storage, 'cgroup': cgroup, 'containers': containers, 'memory_total': mem['MemTotal'],
            'memory_available': mem['MemAvailable']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project')
    parser.add_argument('--mount-only', action='store_true',
                        help='Bootstrap check only: does not establish processing capacity')
    parser.add_argument('--storage-root', type=Path, required=True)
    parser.add_argument('--expected-uuid', required=True)
    parser.add_argument('--expected-hostname', default='<LOCAL_HOSTNAME>')
    parser.add_argument('--min-storage-gib', type=int, default=40)
    parser.add_argument('--min-root-gib', type=int, default=40)
    parser.add_argument('--min-inodes', type=int, default=100000)
    parser.add_argument('--max-workers', type=int, default=2)
    parser.add_argument('--requested-workers', type=int, default=0)
    parser.add_argument('--host-reserve-gib', type=int, default=2)
    args = parser.parse_args()
    if ((not args.mount_only and not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', args.project or ''))
            or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', args.expected_uuid)
            or min(args.min_storage_gib, args.min_root_gib, args.min_inodes, args.max_workers, args.host_reserve_gib) <= 0
            or args.requested_workers < 0):
        parser.error('Invalid identity or finite capacity limits')
    try:
        limits = vars(args).copy()
        del limits['project'], limits['storage_root'], limits['mount_only']
        if args.mount_only:
            for key in ('max_workers', 'requested_workers', 'host_reserve_gib'):
                del limits[key]
            failures = evaluate_storage(collect_storage(args.storage_root), **limits)
        else:
            failures = evaluate_snapshot(collect_snapshot(args.project, args.storage_root), **limits)
    except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError, subprocess.SubprocessError):
        print('Storage/capacity evidence unavailable or invalid; refusing readiness', file=sys.stderr)
        return 1
    print(json.dumps({'ready': not failures, 'capacity_checked': not args.mount_only,
                      'failures': failures, 'country_start_authorized': False}))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
