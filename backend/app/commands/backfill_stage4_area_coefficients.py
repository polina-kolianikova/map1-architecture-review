from __future__ import annotations

import argparse
import json

from app.db.session import SessionLocal
from app.services.stage4_automation import backfill_area_coefficients


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Stage 4 area coefficients")
    parser.add_argument("--table", choices=["grid_cells", "grid_cells_partitioned"], default="grid_cells")
    parser.add_argument("--region-code")
    parser.add_argument("--grid-version")
    parser.add_argument("--batch-size", type=int, default=100_000)
    parser.add_argument("--max-batches", type=int)
    args = parser.parse_args()

    with SessionLocal() as db:
        report = backfill_area_coefficients(
            db,
            table_name=args.table,
            region_code=args.region_code,
            grid_version=args.grid_version,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
