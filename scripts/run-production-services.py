#!/usr/bin/env python3
"""Supervise the Map 1 application only. Processing services are never started."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import time

APP = Path('/srv/map1/app')
STORAGE = Path('/srv/map1/storage/production')
UUID = '<LOCAL_STORAGE_UUID>'
SERVICES = ('db', 'redis', 'backend', 'frontend', 'caddy')
PROCESSING_JOBS = ('stage1-tools', 'stage4-worker')
COMPOSE = ['docker', 'compose', '--project-name', 'map1', '--env-file', str(APP / 'runtime.env'),
           '-f', str(APP / 'infra/docker-compose.prod.yml'),
           '-f', str(APP / 'infra/docker-compose.capacity.yml'),
           '-f', str(APP / 'deploy.override.json')]
CLOSING = False


def command(args, timeout=60):
    process = subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                output, error = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                starting = args[:len(COMPOSE)] == COMPOSE and 'up' in args[len(COMPOSE):]
                if time.monotonic() >= deadline or (starting and CLOSING):
                    raise RuntimeError('Project command cancelled or timed out')
    except BaseException:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        raise
    if process.returncode:
        # Configuration failures must not put resolved credentials into a journal.
        raise RuntimeError('Project service command failed (exit %s)' % process.returncode)
    return output.strip()


def check_storage():
    command(['python3', str(APP / 'scripts/check-production-storage.py'), '--mount-only',
             '--storage-root', str(STORAGE), '--expected-uuid', UUID])
    pg = STORAGE / 'postgres'
    if pg.is_symlink() or (pg / 'PG_VERSION').read_text().strip() != '16':
        raise RuntimeError('Verified PostgreSQL 16 cluster is required')
    for path in (pg, STORAGE / 'tablespace'):
        if path.is_symlink() or not path.is_dir() or path.stat().st_dev != STORAGE.stat().st_dev:
            raise RuntimeError('Database storage identity mismatch')


def processing_authorized():
    approved = APP / 'processing-approved.json'
    try:
        if not approved.is_file() or approved.is_symlink():
            return False
        # Check the opened file itself; a replacement symlink must not grant
        # approval, and a replacement FIFO must not block the supervisor.
        def open_approval(path, flags):
            return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
        with open(approved, encoding='utf-8', opener=open_approval) as handle:
            metadata = os.fstat(handle.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                    or metadata.st_mode & 0o077):
                return False
            approval = json.load(handle)
        return isinstance(approval, dict) and approval.get('authorized') is True
    except (OSError, ValueError):
        return False


def check_inventory():
    ids = command(['docker', 'ps', '--no-trunc', '-q', '--filter', 'label=com.docker.compose.project=map1']).split()
    if not ids:
        return
    values = json.loads(command(['docker', 'inspect', *ids]))
    processing = []
    jobs = beats = 0
    for container in values:
        labels = container['Config']['Labels']
        service = labels.get('com.docker.compose.service')
        name = container['Name']
        if service in SERVICES:
            if name != '/map1-' + service + '-1':
                raise RuntimeError('Unexpected or unauthorized active project container')
            continue
        if service in PROCESSING_JOBS:
            names = ('/map1-' + service + '-1', '/map1-' + service + '-2')
            jobs += 1
        elif service == 'stage4-beat':
            names = ('/map1-stage4-beat-1',)
            beats += 1
        else:
            raise RuntimeError('Unexpected or unauthorized active project container')
        if name not in names or labels.get('com.docker.compose.project') != 'map1':
            raise RuntimeError('Unexpected or unauthorized active project container')
        processing.append(container)
    if (jobs > 2 or beats > 1
            or len({c['Name'] for c in processing}) != len(processing)):
        raise RuntimeError('Processing inventory exceeds two jobs and one beat')
    if processing and not processing_authorized():
        raise RuntimeError('Processing requires a safe explicit approval')
    # The separate capacity admission gate checks actual kernel slice ceilings.
    # Inventory permission never starts work or replaces that admission check.
    for container in processing:
        config = container.get('HostConfig', {})
        if (config.get('CgroupParent') != 'map1-workers.slice'
                or config.get('RestartPolicy', {}).get('Name') != 'no'
                or any(type(config.get(key)) is not int or config[key] <= 0
                       for key in ('NanoCpus', 'Memory', 'PidsLimit'))):
            raise RuntimeError('Processing requires finite CPU/RAM/PIDs, worker slice and no restart')


def validate_configuration():
    # Include inactive profiles for validation only; startup still names exactly
    # the five application services and never enables a processing profile.
    model = json.loads(command(COMPOSE + ['--profile', '*', 'config', '--format', 'json']))
    for service in SERVICES:
        config = model['services'][service]
        if config.get('restart') != 'no' or min(float(config.get(k, 0)) for k in ('cpus', 'mem_limit', 'pids_limit')) <= 0:
            raise RuntimeError('Unbounded service or automatic Docker restart')
    db = model['services']['db']
    if db.get('environment', {}).get('PGDATA', '/var/lib/postgresql/data') != '/var/lib/postgresql/data':
        raise RuntimeError('PostgreSQL data directory redirection is forbidden')
    if db.get('command') not in (None, [], ['postgres']) or db.get('entrypoint'):
        raise RuntimeError('Unexpected database startup command')
    backend_command = model['services']['backend'].get('command', [])
    if (backend_command[:7] != ['uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000', '--workers']
            or len(backend_command) != 8 or str(backend_command[7]) not in ('1', '2')
            or model['services']['backend'].get('entrypoint')):
        raise RuntimeError('Unexpected API startup command')
    mounts = {v['target']:v for v in db['volumes']}
    for target, name in (('/var/lib/postgresql/data', 'postgres'),
                         ('/var/lib/postgresql/stage4_tablespace', 'tablespace')):
        mount = mounts[target]
        if mount['source'] != str(STORAGE / name) or mount['type'] != 'bind' or mount.get('bind', {}).get('create_host_path', True):
            raise RuntimeError('Unverified database mount in final configuration')
    override = json.loads((APP / 'deploy.override.json').read_text())
    for service in ('backend', 'frontend'):
        if model['services'][service]['image'] != override['services'][service]['image']:
            raise RuntimeError('Release image mismatch')
    if model['services']['backend']['environment']['STAGE4_SCHEDULER_ENABLED'] != 'false':
        raise RuntimeError('Scheduler must remain disabled')
    for name in ('stage4-worker', 'stage4-beat'):
        if 'processing' not in model['services'][name].get('profiles', []):
            raise RuntimeError('Processing must require an explicit profile')


def inspect_services():
    check_inventory()
    values = json.loads(command(['docker', 'inspect', *['map1-' + s + '-1' for s in SERVICES]]))
    if len(values) != len(SERVICES):
        raise RuntimeError('Incomplete project service inspection')
    for service, value in zip(SERVICES, values):
        labels = value['Config']['Labels']
        if labels.get('com.docker.compose.project') != 'map1' or labels.get('com.docker.compose.service') != service:
            raise RuntimeError('Unexpected project service identity')
        if value['HostConfig']['RestartPolicy']['Name'] != 'no':
            raise RuntimeError('Docker restart bypasses the storage gate')
        if service == 'db':
            mounts = {m['Destination']:m['Source'] for m in value['Mounts']}
            if mounts.get('/var/lib/postgresql/data') != str(STORAGE / 'postgres'):
                raise RuntimeError('Database is outside the verified production volume')
        state = value['State']
        if not state['Running'] or state.get('Health', {}).get('Status') != 'healthy':
            raise RuntimeError('Project service is not healthy: ' + service)
    return values


def stop():
    # Exact service names deliberately exclude workers, tools and rehearsal data.
    try:
        command(COMPOSE + ['stop', '--timeout', '60', *SERVICES], timeout=70)
    except RuntimeError:
        ids = command(['docker', 'ps', '--no-trunc', '-q', '--filter',
                       'label=com.docker.compose.project=map1'], timeout=10).split()
        if not ids:
            return
        values = json.loads(command(['docker', 'inspect', *ids], timeout=10))
        targets = []
        for value in values:
            labels = value['Config']['Labels']
            service = labels.get('com.docker.compose.service')
            if service not in SERVICES:
                continue
            if labels.get('com.docker.compose.project') != 'map1' or value['Name'] != '/map1-' + service + '-1':
                raise RuntimeError('Cannot establish safe shutdown targets')
            targets.append(value['Id'])
        if targets:
            command(['docker', 'stop', '--time', '60', *targets], timeout=70)
            remaining = json.loads(command(['docker', 'inspect', *targets], timeout=10))
            if any(c['State']['Running'] for c in remaining):
                raise RuntimeError('Application shutdown was not confirmed')


def main():
    global CLOSING
    CLOSING = False
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'stop'))
    args = parser.parse_args()
    if socket.gethostname() != '<LOCAL_HOSTNAME>':
        raise SystemExit('Unexpected project server')
    if args.action == 'stop':
        stop()
        return
    with open('/run/lock/map1-production.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_storage()
        # Validate before mutations; --no-deps and the explicit service list
        # prohibit starting Celery, tools or migrations through this supervisor.
        validate_configuration()
        check_inventory()
        closing = False
        def terminate(signum, frame):
            nonlocal closing
            global CLOSING
            closing = True
            CLOSING = True
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        try:
            if closing:
                return
            command(COMPOSE + ['up', '--detach', '--no-build', '--no-deps', '--wait',
                               '--wait-timeout', '120', *SERVICES], timeout=180)
            while not closing:
                check_storage()
                inspect_services()
                for _ in range(30):
                    if closing:
                        break
                    time.sleep(1)
        finally:
            stop()


if __name__ == '__main__':
    main()
