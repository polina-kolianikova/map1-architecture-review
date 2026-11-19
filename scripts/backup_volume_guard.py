#!/usr/bin/env python3
"""Configured host-side backup mount admission and writer supervision.

No mount, provider, service or configuration changes. Configure BOTH mount and
UUID in runtime/job env files for scheduled and manual entrypoints. Unconfigured
legacy jobs retain their behavior; incomplete guard configuration fails closed.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

GIB = 1024**3
NAMES = ("BACKUP_VOLUME_MOUNT", "BACKUP_VOLUME_UUID", "BACKUP_EXTRA_RESERVE_PATH",
         "BACKUP_MIN_FREE_GB", "BACKUP_MIN_FREE_INODES", "BACKUP_MAX_DUMP_GB")


class GuardError(Exception):
    pass


def require(ok, message):
    if not ok:
        raise GuardError(message)


def settings(env_file=None, environ=None):
    environ = os.environ if environ is None else environ
    values = {}
    if env_file and Path(env_file).is_file():
        for line in Path(env_file).read_text().splitlines():
            key, sep, value = line.partition("=")
            if not sep or key not in NAMES:
                continue
            require(key not in values, "Duplicate backup guard setting")
            value = value.strip()
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            values[key] = value
    values.update({k: environ[k] for k in NAMES if k in environ})
    mount, identity = values.get(NAMES[0], ""), values.get(NAMES[1], "")
    if not mount and not identity:
        require(not values.get("BACKUP_EXTRA_RESERVE_PATH"), "Extra reserve requires a configured volume")
        return None
    require(mount and re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", identity),
            "Both backup volume mount and UUID are required")
    mount = Path(mount)
    extra = Path(values.get("BACKUP_EXTRA_RESERVE_PATH", "/"))
    require(mount.is_absolute() and extra.is_absolute() and mount != Path("/"),
            "Backup reserve paths must be absolute; volume must not be root")
    def number(key, default, minimum):
        value = values.get(key, default)
        require(re.fullmatch(r"[0-9]{1,8}", value or "") is not None, "Invalid backup guard bound")
        value = int(value)
        require(value >= minimum, "Backup guard bound is below its minimum")
        return value
    return {"mount": mount, "uuid": identity.lower(), "extra": extra,
            "min_bytes": number("BACKUP_MIN_FREE_GB", "40", 40) * GIB,
            "min_inodes": number("BACKUP_MIN_FREE_INODES", "100000", 100000),
            "max_bytes": number("BACKUP_MAX_DUMP_GB", "", 1) * GIB}


def canonical(path, allow_missing=False):
    require(path.is_absolute() and ".." not in path.parts, "Unsafe backup path")
    require(path.resolve() == path, "Backup path may not traverse symlinks")
    for p in (path, *path.parents):
        if not p.exists():
            require(allow_missing and not p.is_symlink(), "Backup reserve directory is missing")
            continue
        require(not p.is_symlink() and p.is_dir(), "Unsafe backup directory")


def check(config, directory):
    if config is None:
        return
    mount, extra, directory = config["mount"], config["extra"], Path(directory)
    canonical(mount)
    canonical(extra)
    canonical(directory, allow_missing=True)
    require(directory.is_relative_to(mount) and directory != mount,
            "Guarded backup directory must be below its configured volume")
    require(os.path.ismount(mount) and mount.stat().st_dev != Path("/").stat().st_dev,
            "Backup volume is not a separate mounted filesystem")
    try:
        p = subprocess.run(["findmnt", "-J", "-T", str(mount), "-o", "TARGET,UUID,OPTIONS"],
                           capture_output=True, text=True, timeout=5)
        row = json.loads(p.stdout)["filesystems"][0]
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired):
        raise GuardError("Backup mount identity unavailable") from None
    require(p.returncode == 0 and row["target"] == str(mount)
            and row["uuid"].lower() == config["uuid"] and "rw" in row["options"].split(","),
            "Backup volume UUID, mount or writable state does not match")
    ancestor = directory
    while not ancestor.exists():
        ancestor = ancestor.parent
    require(ancestor.stat().st_dev == mount.stat().st_dev, "Backup destination filesystem mismatch")
    for path in {mount, extra, Path("/")}:
        v = os.statvfs(path)
        require(v.f_bavail * v.f_frsize >= config["min_bytes"], "Backup filesystem byte reserve breached")
        require(v.f_favail >= config["min_inodes"], "Backup filesystem inode reserve breached")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--writer", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        config = settings(args.env_file)
        check(config, args.directory)
        if not args.writer:
            require(not args.command, "Unexpected guard command")
            return 0
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        require(bool(command), "Writer command required")
        if config is None:
            os.execvp(command[0], command)
        from backup_budget import run_guard
        return run_guard(command, directory=args.directory,
                         min_free_bytes=config["min_bytes"], min_free_inodes=config["min_inodes"],
                         max_file_bytes=config["max_bytes"], reserve_paths=(str(config["extra"]),),
                         storage_check=lambda: check(config, args.directory))
    except (GuardError, OSError, ValueError):
        print("Backup storage guard refused; check configured mount identity and reserves", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
