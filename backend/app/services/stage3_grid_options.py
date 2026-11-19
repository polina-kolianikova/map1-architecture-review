from __future__ import annotations

from typing import Any

MAX_GRID_CELLS_PER_TILE = 100_000


def projected_srid_for_lon_lat(lon: float, lat: float) -> int:
    if lon < -180 or lon > 180:
        raise ValueError("longitude must be between -180 and 180")
    if lat < -90 or lat > 90:
        raise ValueError("latitude must be between -90 and 90")
    zone = int((lon + 180) // 6) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def validate_generation_args(args) -> None:
    if args.grid_size_m <= 0:
        raise ValueError("grid_size_m must be greater than zero")
    if args.tile_size_m <= 0:
        raise ValueError("tile_size_m must be greater than zero")
    if args.tile_size_m % args.grid_size_m != 0:
        raise ValueError("tile_size_m must be an integer multiple of grid_size_m")
    if args.projected_srid < 0:
        raise ValueError("projected_srid must be positive or 0 for auto")
    if args.max_tiles < 0:
        raise ValueError("max_tiles must not be negative")
    if getattr(args, "max_failed_tiles", 0) < 0:
        raise ValueError("max_failed_tiles must not be negative")
    if getattr(args, "statement_timeout_ms", 0) < 0:
        raise ValueError("statement_timeout_ms must not be negative")
    if args.max_tile_attempts <= 0:
        raise ValueError("max_tile_attempts must be greater than zero")
    estimated_cells_per_tile = (args.tile_size_m / args.grid_size_m) ** 2
    if estimated_cells_per_tile > MAX_GRID_CELLS_PER_TILE:
        raise ValueError("tile_size_m is too large for the selected grid_size_m")


def validate_existing_run(run: dict[str, Any], args) -> None:
    expected = {
        "grid_size_m": args.grid_size_m,
        "projected_srid": args.projected_srid,
        "tile_size_m": args.tile_size_m,
    }
    mismatches = [
        name
        for name, expected_value in expected.items()
        if int(run[name]) != int(expected_value)
    ]
    if mismatches:
        raise RuntimeError(f"Existing grid run has different parameters: {', '.join(mismatches)}")
