"""Publish a finite set of exact overview tiles without changing GIS records."""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from sqlalchemy import event, text
from starlette.requests import Request

from app.api import stage2
from app.db.session import engine
from app.services.forest_scope import component_extent_info, require_component_extent_publication
from app.services.stage2_tile_cache import TileCache, mask_revision, mask_tile_key, tile_key_digest


def write_atomic(path, raw):
    temporary = path.with_suffix(path.suffix + '.pending-' + str(os.getpid()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region-code', required=True)
    parser.add_argument('--grid-version', required=True)
    parser.add_argument('--tile', action='append', required=True, help='z/x/y; at most sixteen overview tiles')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--statement-timeout-ms', type=int, default=240000)
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.output_dir.is_absolute():
        parser.error('A project administrator must publish to an absolute artifact path')
    if not 1 <= len(args.tile) <= 16 or not 1000 <= args.statement_timeout_ms <= 240000:
        parser.error('Finite tile and statement budgets are required')
    tiles = []
    for value in args.tile:
        try:
            z, x, y = (int(part) for part in value.split('/'))
        except ValueError:
            parser.error('Each tile must be z/x/y')
        stage2._validate_tile(z, x, y)
        if not 0 <= z <= 12:
            parser.error('Only overview tiles can be published')
        tiles.append((z, x, y))
    for directory in (args.output_dir.parent, args.output_dir):
        metadata = directory.lstat()
        if directory.is_symlink() or not directory.is_dir() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            parser.error('Published tile directories must be owned and writable only by root')

    def readonly_begin(connection):
        connection.exec_driver_sql('SET TRANSACTION READ ONLY')
        connection.exec_driver_sql('SET LOCAL statement_timeout = ' + str(args.statement_timeout_ms))
        connection.exec_driver_sql('SET LOCAL max_parallel_workers_per_gather = 0')
    event.listen(engine, 'begin', readonly_begin)
    stage2.MASK_TILE_CACHE = TileCache(published_dir=args.output_dir)
    request = Request({'type': 'http', 'headers': [(b'accept-encoding', b'identity')]})
    try:
        for z, x, y in dict.fromkeys(tiles):
            started = time.monotonic()
            with engine.connect() as db:
                row = db.execute(text('SELECT id, metadata FROM regions WHERE code = :code'),
                                 {'code': args.region_code}).mappings().one()
                info = component_extent_info(row['metadata'])
                if info is None:
                    raise RuntimeError('An authoritative published component scope is required')
                require_component_extent_publication(db, region_id=row['id'], metadata=row['metadata'])
                revision = mask_revision(row['metadata'])
            key = mask_tile_key(revision, row['id'], info['mask_version'], args.grid_version,
                                info['projected_srid'], z, x, y)
            response = stage2.mask_tile(z, x, y, region_code=args.region_code,
                                       grid_version=args.grid_version, request=request)
            if response.status_code != 200 or response.headers.get('content-encoding'):
                raise RuntimeError('An exact identity MVT response is required')
            raw = response.body
            if len(raw) > 64 * 1024 * 1024:
                raise RuntimeError('Published tile exceeds the bounded response budget')
            with engine.connect() as db:
                current = db.execute(text('SELECT metadata FROM regions WHERE id = :id'),
                                     {'id': row['id']}).scalar_one()
                if mask_revision(current) != revision:
                    raise RuntimeError('Mask publication changed while the tile was prepared')
            digest = tile_key_digest(key)
            write_atomic(args.output_dir / (digest + '.mvt'), raw)
            receipt = {'schema': 1, 'key_sha256': digest, 'size': len(raw),
                       'content_sha256': hashlib.sha256(raw).hexdigest(), 'encoding': 'identity',
                       'tile': [z, x, y], 'revision': revision}
            write_atomic(args.output_dir / (digest + '.json'), (json.dumps(receipt) + '\n').encode())
            directory_fd = os.open(args.output_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            print(json.dumps({'tile': [z, x, y], 'bytes': len(raw), 'seconds': round(time.monotonic()-started, 3),
                              'published': True, 'database_changed': False}), flush=True)
    finally:
        event.remove(engine, 'begin', readonly_begin)


if __name__ == '__main__':
    main()
