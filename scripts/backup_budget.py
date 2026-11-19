"""Run one owned backup process group with a continuing filesystem reserve.

Only the launched POSIX process group is signalled. This does not manage work
that a daemon (for example Docker) starts outside that group. No files are pruned
or deleted here; the caller retains its existing staging cleanup responsibility.
"""
import argparse
import math
import os
import resource
import signal
import subprocess
import sys
import time


class BudgetError(Exception):
    pass


def check_reserve(directory, min_free_bytes, min_free_inodes):
    try:
        info = os.statvfs(directory)
    except OSError:
        raise BudgetError('Cannot check backup filesystem reserve') from None
    if info.f_bavail * info.f_frsize < min_free_bytes:
        raise BudgetError('Backup filesystem free-space reserve breached')
    if info.f_favail < min_free_inodes:
        raise BudgetError('Backup filesystem inode reserve breached')


def group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass


def stop_owned_group(child, signum, grace_seconds):
    """Reap our child and stop remaining group members within finite deadlines."""
    pgid = child.pid  # start_new_session=True: never use the caller's group.
    child.poll()
    if group_exists(pgid):
        signal_group(pgid, signum)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            child.poll()
            if not group_exists(pgid):
                break
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        if group_exists(pgid):
            signal_group(pgid, signal.SIGKILL)
    try:
        child.wait(timeout=1)
        return True
    except subprocess.TimeoutExpired:
        return False


def run_guard(command, *, directory, min_free_bytes, min_free_inodes,
              max_file_bytes, interval_seconds=1.0, grace_seconds=5.0,
              reserve_paths=(), storage_check=None):
    if (not command or min_free_bytes < 0 or min_free_inodes < 0 or max_file_bytes <= 0
            or not math.isfinite(interval_seconds) or not 0 < interval_seconds <= 1
            or not math.isfinite(grace_seconds) or not 0 <= grace_seconds <= 30):
        raise ValueError('Invalid backup budget arguments')
    # Additional host paths are explicit. Never create a missing mount directory
    # or silently treat an unavailable filesystem as an empty list of reserves.
    reserve_paths = tuple(reserve_paths)
    if any(not os.path.isabs(path) for path in reserve_paths):
        raise ValueError('Reserve paths must be absolute')
    devices = {}

    def check_all_reserves():
        if storage_check is not None:
            try:
                storage_check()
            except Exception:
                raise BudgetError('Configured backup volume or reserve check failed') from None
        check_reserve(directory, min_free_bytes, min_free_inodes)
        for path in reserve_paths:
            try:
                if not os.path.isdir(path):
                    raise OSError('Reserve directory unavailable')
                device = os.stat(path).st_dev
            except OSError:
                raise BudgetError('Cannot check additional filesystem reserve') from None
            if device != devices.setdefault(path, device):
                raise BudgetError('Additional reserve filesystem changed during backup')
            check_reserve(path, min_free_bytes, min_free_inodes)
    received = []
    watched = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
    previous = {}
    child = None
    status = 1

    def remember_signal(signum, _frame):
        if not received:
            received.append(signum)

    def set_file_limit():
        # The guard is single-threaded. Only the new child receives the limit;
        # its shell, pipeline and encryption subprocesses inherit it as before.
        _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        ceiling = max_file_bytes if hard == resource.RLIM_INFINITY else min(max_file_bytes, hard)
        resource.setrlimit(resource.RLIMIT_FSIZE, (ceiling, ceiling))

    try:
        for signum in watched:
            previous[signum] = signal.signal(signum, remember_signal)
        check_all_reserves()
        if received:
            return 128 + received[0]
        child = subprocess.Popen(command, start_new_session=True, preexec_fn=set_file_limit)
        next_check = time.monotonic()
        while True:
            if received:
                status = 128 + received[0]
                break
            now = time.monotonic()
            if now >= next_check:
                check_all_reserves()
                next_check = now + interval_seconds
            result = child.poll()
            if result is not None:
                # A short command may finish between samples. Never accept a
                # successful dump without checking its resulting free space.
                check_all_reserves()
                status = result if result >= 0 else 128 - result
                break
            time.sleep(min(0.05, max(0, next_check - time.monotonic())))
    except BudgetError as exc:
        print(str(exc), file=sys.stderr)
        status = 1
    except (OSError, subprocess.SubprocessError):
        # Do not echo command arguments, environment, or exception details.
        print('Backup subprocess could not be started or supervised', file=sys.stderr)
        status = 1
    finally:
        try:
            if child is not None and not stop_owned_group(
                    child, received[0] if received else signal.SIGTERM, grace_seconds):
                print('Backup subprocess did not exit after bounded cleanup', file=sys.stderr)
                status = 1
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    return 128 + received[0] if received else status


def nonnegative(value):
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError('Expected a nonnegative integer')
    return int(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--reserve-path', action='append', default=[],
                        help='Additional absolute host directory to monitor; repeat for each filesystem. '
                             'Uses the same byte/inode floors; does not verify the initial mount UUID.')
    parser.add_argument('--min-free-bytes', required=True, type=nonnegative)
    parser.add_argument('--min-free-inodes', type=nonnegative, default=100000)
    parser.add_argument('--max-file-bytes', required=True, type=nonnegative)
    parser.add_argument('--interval-seconds', type=float, default=1.0)
    parser.add_argument('--grace-seconds', type=float, default=5.0)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    # Also support host wrappers using runpy, which do not add this script's
    # directory to sys.path as normal CLI execution does.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from backup_volume_guard import settings, check, GuardError
    except ImportError:
        parser.error('Backup storage guard module is required beside the budget helper')
    try:
        storage = settings(os.environ.get('ENV_FILE'))
        return run_guard(command, directory=args.directory, min_free_bytes=args.min_free_bytes,
                         min_free_inodes=args.min_free_inodes, max_file_bytes=args.max_file_bytes,
                         interval_seconds=args.interval_seconds, grace_seconds=args.grace_seconds,
                         reserve_paths=args.reserve_path,
                         storage_check=(lambda: check(storage, args.directory)) if storage else None)
    except (ValueError, GuardError):
        parser.error('Invalid backup budget arguments')


if __name__ == '__main__':
    raise SystemExit(main())
