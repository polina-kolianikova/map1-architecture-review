import json
from argparse import ArgumentParser, SUPPRESS
from datetime import UTC, datetime, timedelta
from datetime import date as Date
from time import sleep

import requests
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.cdse import CdseClient, SentinelProduct


DEFAULT_LOOKBACK_DAYS = 45
END = datetime.now(UTC)
START = END - timedelta(days=DEFAULT_LOOKBACK_DAYS)
MAX_CLOUD_COVERAGE = 40
DEFAULT_CELL_LIMIT = None
MIN_VALID_PIXEL_RATIO = 0.35
DEFAULT_PIPELINE_NAME = "stage1_cdse_ndvi"


def main() -> None:
    args = parse_args()
    start = _parse_day(args.start)
    end = _parse_day(args.end, end_of_day=True)

    settings = get_settings()
    region_code = args.region_code or settings.active_region_code
    grid_version = args.grid_version or settings.active_grid_version
    with SessionLocal() as db:
        region_wkt = load_region_wkt(db, region_code)

    client = CdseClient(settings, timeout=args.cdse_timeout)
    token = None
    products: list[SentinelProduct] = []
    source_error = None
    try:
        token = client.get_access_token()
        products = client.search_sentinel2_products(
            token,
            start=start,
            end=end,
            max_cloud_coverage=args.max_cloud,
            area_wkt=region_wkt,
        )
    except (requests.RequestException, RuntimeError) as exc:
        source_error = _short_error(exc)
        print(f"cdse ndvi source unavailable error={source_error}")

    if source_error is not None and not args.allow_source_unavailable:
        raise SystemExit(1)

    with SessionLocal() as db:
        lock_cdse_refresh(db, region_code, grid_version, args.pipeline_name)
        inserted = 0
        if source_error is not None:
            cells = load_grid_cells(
                db,
                args.cell_limit,
                region_code,
                grid_version,
                args.cell_uuid,
                args.cell_offset,
            )
            inserted = store_unavailable_observations(
                db,
                cells=cells,
                region_code=region_code,
                grid_version=grid_version,
                max_cloud_coverage=args.max_cloud,
                fallback_date=end.date(),
                error=source_error,
                min_valid_pixel_ratio=args.min_valid_pixel_ratio,
                history_mode=args.history_mode,
                pipeline_name=args.pipeline_name,
            )
            db.commit()
            print(
                "cdse ndvi stored "
                f"products=0 cells={len(cells)} observations={inserted} "
                f"period={start.date()}:{end.date()}"
            )
            return

        cells = load_grid_cells(
            db,
            args.cell_limit,
            region_code,
            grid_version,
            args.cell_uuid,
            args.cell_offset,
        )
        if not args.history_mode and should_retire_current_observations(args.cell_limit, args.cell_uuid):
            retire_current_observations(db, region_code, grid_version, args.pipeline_name)
        failed_cell_count = 0
        total_cells = len(cells)
        for index, cell in enumerate(cells, start=1):
            if should_report_progress(index, total_cells):
                print(
                    "cdse ndvi progress "
                    f"cells={index}/{total_cells} "
                    f"observations={inserted} "
                    f"failed={failed_cell_count}",
                    flush=True,
                )
            try:
                statistics = client.request_ndvi_statistics(
                    token,
                    start=start,
                    end=end,
                    max_cloud_coverage=args.max_cloud,
                    geometry=cell["geometry"],
                    resolution=args.resolution,
                )
            except (requests.RequestException, RuntimeError) as exc:
                print(
                    "cdse ndvi cell unavailable "
                    f"cell_uuid={cell['uuid']} error={_short_error(exc)}"
                )
                inserted += store_unavailable_observation(
                    db,
                    cell_id=cell["id"],
                    cell_uuid=cell["uuid"],
                    region_code=region_code,
                    grid_version=grid_version,
                    max_cloud_coverage=args.max_cloud,
                    fallback_date=end.date(),
                    error=_short_error(exc),
                    min_valid_pixel_ratio=args.min_valid_pixel_ratio,
                    request_start=start,
                    request_end=end,
                    request_resolution=args.resolution,
                    history_mode=args.history_mode,
                    pipeline_name=args.pipeline_name,
                )
                failed_cell_count += 1
                if args.request_delay:
                    sleep(args.request_delay)
                continue
            try:
                inserted += store_observations(
                    db,
                    products,
                    statistics,
                    cell_id=cell["id"],
                    cell_uuid=cell["uuid"],
                    region_code=region_code,
                    grid_version=grid_version,
                    max_cloud_coverage=args.max_cloud,
                    fallback_date=end.date(),
                    min_valid_pixel_ratio=args.min_valid_pixel_ratio,
                    request_start=start,
                    request_end=end,
                    request_resolution=args.resolution,
                    history_mode=args.history_mode,
                    pipeline_name=args.pipeline_name,
                )
            except (KeyError, TypeError, ValueError) as exc:
                print(
                    "cdse ndvi cell invalid "
                    f"cell_uuid={cell['uuid']} error={_short_error(exc)}"
                )
                inserted += store_unavailable_observation(
                    db,
                    cell_id=cell["id"],
                    cell_uuid=cell["uuid"],
                    region_code=region_code,
                    grid_version=grid_version,
                    max_cloud_coverage=args.max_cloud,
                    fallback_date=end.date(),
                    error=_short_error(exc),
                    min_valid_pixel_ratio=args.min_valid_pixel_ratio,
                    request_start=start,
                    request_end=end,
                    request_resolution=args.resolution,
                    history_mode=args.history_mode,
                    pipeline_name=args.pipeline_name,
                )
                failed_cell_count += 1
            if args.request_delay:
                sleep(args.request_delay)

        if cells and failed_cell_count == len(cells):
            if not args.allow_source_unavailable:
                raise SystemExit(1)
            inserted = store_unavailable_observations(
                db,
                cells=cells,
                region_code=region_code,
                grid_version=grid_version,
                max_cloud_coverage=args.max_cloud,
                fallback_date=end.date(),
                error="Sentinel Hub statistics unavailable for all sampled cells",
                min_valid_pixel_ratio=args.min_valid_pixel_ratio,
                history_mode=args.history_mode,
                pipeline_name=args.pipeline_name,
            )
        db.commit()

    print(
        "cdse ndvi stored "
        f"products={len(products)} cells={len(cells)} observations={inserted} "
        f"period={start.date()}:{end.date()}"
    )


def parse_args():
    parser = ArgumentParser()
    parser.add_argument("--start", default=START.date().isoformat())
    parser.add_argument("--end", default=END.date().isoformat())
    parser.add_argument("--max-cloud", type=int, default=MAX_CLOUD_COVERAGE)
    parser.add_argument("--cell-limit", type=int, default=DEFAULT_CELL_LIMIT)
    parser.add_argument("--cell-offset", type=int, default=0)
    parser.add_argument("--resolution", type=float, default=0.0001)
    parser.add_argument("--request-delay", type=float, default=1.0)
    parser.add_argument("--cdse-timeout", type=int, default=30)
    parser.add_argument("--min-valid-pixel-ratio", type=float, default=MIN_VALID_PIXEL_RATIO)
    parser.add_argument("--allow-source-unavailable", action="store_true")
    parser.add_argument("--fail-on-source-unavailable", action="store_true", help=SUPPRESS)
    parser.add_argument("--history-mode", action="store_true")
    parser.add_argument("--pipeline-name", default=DEFAULT_PIPELINE_NAME)
    parser.add_argument("--region-code")
    parser.add_argument("--grid-version")
    parser.add_argument("--cell-uuid", default="")
    args = parser.parse_args()
    if args.cell_offset < 0:
        raise ValueError("cell-offset must not be negative")
    if args.cell_uuid.strip() and args.cell_offset:
        raise ValueError("cell-offset cannot be combined with cell-uuid")
    return args


def should_retire_current_observations(cell_limit: int | None, cell_uuid: str = "") -> bool:
    return cell_limit is None and not cell_uuid.strip()


def load_region_wkt(db, region_code: str) -> str:
    wkt = db.execute(
        text(
            """
            SELECT ST_AsText(ST_Envelope(geom))
            FROM regions
            WHERE code = :region_code
            """
        ),
        {"region_code": region_code},
    ).scalar_one_or_none()
    if wkt is None:
        raise RuntimeError(f"region {region_code} does not exist")
    return wkt


def load_grid_cells(
    db,
    limit: int | None,
    region_code: str,
    grid_version: str,
    cell_uuid: str = "",
    offset: int = 0,
) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT
                cell.id,
                cell.uuid::text AS uuid,
                ST_AsGeoJSON(cell.geom)::json AS geometry
            FROM grid_cells cell
            WHERE cell.region_id = (
                  SELECT id
                  FROM regions
                  WHERE code = :region_code
              )
              AND cell.grid_version = :grid_version
              AND (:cell_uuid = '' OR cell.uuid::text = :cell_uuid)
            ORDER BY cell.id
            LIMIT :limit
            OFFSET :offset
            """
        ),
        {
            "region_code": region_code,
            "grid_version": grid_version,
            "cell_uuid": cell_uuid.strip(),
            "limit": limit,
            "offset": offset,
        },
    ).mappings()
    cells = [dict(row) for row in rows]
    if cell_uuid and not cells:
        raise RuntimeError(f"grid cell not found: {cell_uuid}")
    return cells


def lock_cdse_refresh(db, region_code: str, grid_version: str, pipeline_name: str = DEFAULT_PIPELINE_NAME) -> None:
    lock_acquired = db.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"{pipeline_name}:{region_code}:{grid_version}"},
    ).scalar_one()
    if not lock_acquired:
        raise RuntimeError("Sentinel/CDSE refresh is already running for this scope")


def store_observations(
    db,
    products: list[SentinelProduct],
    statistics: dict,
    cell_id: int,
    cell_uuid: str,
    region_code: str,
    grid_version: str,
    max_cloud_coverage: int,
    fallback_date: Date | None = None,
    min_valid_pixel_ratio: float = MIN_VALID_PIXEL_RATIO,
    request_start: datetime | None = None,
    request_end: datetime | None = None,
    request_resolution: float | None = None,
    history_mode: bool = False,
    pipeline_name: str = DEFAULT_PIPELINE_NAME,
) -> int:
    product_by_date = {product.acquisition_date: product for product in products}
    region_id = db.execute(
        text("SELECT id FROM regions WHERE code = :code"),
        {"code": region_code},
    ).scalar_one()

    records = []
    for item in statistics.get("data", []):
        interval_start = datetime.fromisoformat(
            item["interval"]["from"].replace("Z", "+00:00")
        ).date()
        stats = (
            item.get("outputs", {})
            .get("ndvi", {})
            .get("bands", {})
            .get("B0", {})
            .get("stats", {})
        )
        ndvi = stats.get("mean")
        sample_count = _as_int(stats.get("sampleCount"))
        no_data_count = _as_int(stats.get("noDataCount"))
        valid_pixel_count = _valid_pixel_count(sample_count, no_data_count, ndvi)
        valid_pixel_ratio = _valid_pixel_ratio(valid_pixel_count, sample_count, ndvi)
        ndvi_status = _ndvi_status(ndvi, valid_pixel_ratio, min_valid_pixel_ratio)
        if ndvi_status != "ok":
            ndvi = None

        product, match_type = _catalogue_match(product_by_date, products, interval_start)
        source_product_id = f"sentinel-hub-statistics-{interval_start.isoformat()}"
        if product is None:
            cloud_coverage = None
            processing_level = "S2MSI2A"
            product_name = None
            catalogue_product_id = None
        else:
            cloud_coverage = product.cloud_coverage
            processing_level = product.processing_level
            product_name = product.name
            catalogue_product_id = product.product_id

        records.append(
            {
                "acquisition_date": interval_start,
                "grid_cell_id": cell_id,
                "region_id": region_id,
                "region_code": region_code,
                "grid_version": grid_version,
                "ndvi": round(float(ndvi), 5) if ndvi is not None else None,
                "cloud_coverage": cloud_coverage,
                "processing_level": processing_level,
                "source_product_id": source_product_id,
                "metadata": _json(
                    {
                        "pipeline": pipeline_name,
                        "region_id": region_id,
                        "region_code": region_code,
                        "grid_version": grid_version,
                        "cell_uuid": cell_uuid,
                        "product_name": product_name,
                        "catalogue_product_id": catalogue_product_id,
                        "catalogue_match_type": match_type,
                        "statistics_source": "sentinel_hub_statistical_api",
                        "interval": item.get("interval", {}),
                        "stats": stats,
                        "geometry_pixel_count": statistics.get("geometryPixelCount"),
                        "sample_count": sample_count,
                        "no_data_count": no_data_count,
                        "valid_pixel_count": valid_pixel_count,
                        "valid_pixel_ratio": valid_pixel_ratio,
                        "min_valid_pixel_ratio": min_valid_pixel_ratio,
                        "max_cloud_coverage": max_cloud_coverage,
                        "statistics_request": _statistics_request_metadata(
                            request_start=request_start,
                            request_end=request_end,
                            request_resolution=request_resolution,
                            max_cloud_coverage=max_cloud_coverage,
                            min_valid_pixel_ratio=min_valid_pixel_ratio,
                        ),
                        "bands": ["B04", "B08", "SCL", "dataMask"],
                        "status": ndvi_status,
                    }
                ),
            }
        )

    if records:
        if not history_mode:
            records_with_ndvi = [record for record in records if record["ndvi"] is not None]
            if records_with_ndvi:
                records = [
                    max(
                        records_with_ndvi,
                        key=lambda record: (
                            record["acquisition_date"],
                            record["source_product_id"],
                        ),
                    )
                ]
            else:
                records = [
                    max(
                        records,
                        key=lambda record: (
                            record["acquisition_date"],
                            record["source_product_id"],
                        ),
                    )
                ]
        current_record = max(
            [record for record in records if record["ndvi"] is not None] or records,
            key=lambda record: (
                record["acquisition_date"],
                record["source_product_id"],
            ),
        )
    else:
        if fallback_date is None:
            return 0
        records = [
            _no_ndvi_record(
                region_id=region_id,
                cell_id=cell_id,
                cell_uuid=cell_uuid,
                region_code=region_code,
                grid_version=grid_version,
                max_cloud_coverage=max_cloud_coverage,
                fallback_date=fallback_date,
                geometry_pixel_count=statistics.get("geometryPixelCount"),
                min_valid_pixel_ratio=min_valid_pixel_ratio,
                request_start=request_start,
                request_end=request_end,
                request_resolution=request_resolution,
                pipeline_name=pipeline_name,
            )
        ]
        current_record = records[0]

    if not history_mode:
        _retire_cell_observations(db, cell_id, region_code, grid_version, pipeline_name)

    for record in records:
        db.execute(
            text(
                """
                INSERT INTO sentinel_observations (
                    grid_cell_id,
                    region_id,
                    region_code,
                    grid_version,
                    acquisition_date,
                    ndvi,
                    cloud_coverage,
                    processing_level,
                    source_product_id,
                    metadata,
                    is_current,
                    last_seen_at
                )
                SELECT
                    :grid_cell_id,
                    :region_id,
                    :region_code,
                    :grid_version,
                    :acquisition_date,
                    :ndvi,
                    :cloud_coverage,
                    :processing_level,
                    :source_product_id,
                    CAST(:metadata AS jsonb),
                    :is_current,
                    now()
                """
            ),
            {
                **record,
                "is_current": (not history_mode) and record is current_record,
            },
        )
    return len(records)


def _no_ndvi_record(
    region_id: int,
    cell_id: int,
    cell_uuid: str,
    region_code: str,
    grid_version: str,
    max_cloud_coverage: int,
    fallback_date: Date,
    geometry_pixel_count: int | None,
    min_valid_pixel_ratio: float,
    request_start: datetime | None = None,
    request_end: datetime | None = None,
    request_resolution: float | None = None,
    pipeline_name: str = DEFAULT_PIPELINE_NAME,
) -> dict:
    return {
        "acquisition_date": fallback_date,
        "grid_cell_id": cell_id,
        "region_id": region_id,
        "region_code": region_code,
        "grid_version": grid_version,
        "ndvi": None,
        "cloud_coverage": None,
        "processing_level": "S2MSI2A",
        "source_product_id": f"sentinel-2-l2a-no-ndvi-{fallback_date.isoformat()}",
        "metadata": _json(
            {
                "pipeline": pipeline_name,
                "region_id": region_id,
                "region_code": region_code,
                "grid_version": grid_version,
                "cell_uuid": cell_uuid,
                "product_name": None,
                "catalogue_product_id": None,
                "catalogue_match_type": "none",
                "statistics_source": "sentinel_hub_statistical_api",
                "interval": {},
                "stats": {},
                "geometry_pixel_count": geometry_pixel_count,
                "sample_count": None,
                "no_data_count": None,
                "valid_pixel_count": 0,
                "valid_pixel_ratio": 0,
                "min_valid_pixel_ratio": min_valid_pixel_ratio,
                "max_cloud_coverage": max_cloud_coverage,
                "statistics_request": _statistics_request_metadata(
                    request_start=request_start,
                    request_end=request_end,
                    request_resolution=request_resolution,
                    max_cloud_coverage=max_cloud_coverage,
                    min_valid_pixel_ratio=min_valid_pixel_ratio,
                ),
                "bands": ["B04", "B08", "SCL", "dataMask"],
                "status": "no_ndvi",
            }
        ),
    }


def store_unavailable_observation(
    db,
    cell_id: int,
    cell_uuid: str,
    region_code: str,
    grid_version: str,
    max_cloud_coverage: int,
    fallback_date: Date,
    error: str,
    min_valid_pixel_ratio: float = MIN_VALID_PIXEL_RATIO,
    request_start: datetime | None = None,
    request_end: datetime | None = None,
    request_resolution: float | None = None,
    history_mode: bool = False,
    pipeline_name: str = DEFAULT_PIPELINE_NAME,
) -> int:
    region_id = db.execute(
        text("SELECT id FROM regions WHERE code = :code"),
        {"code": region_code},
    ).scalar_one()
    if not history_mode:
        _retire_cell_observations(db, cell_id, region_code, grid_version, pipeline_name)
    db.execute(
        text(
            """
            INSERT INTO sentinel_observations (
                grid_cell_id,
                region_id,
                region_code,
                grid_version,
                acquisition_date,
                ndvi,
                cloud_coverage,
                processing_level,
                source_product_id,
                metadata,
                is_current,
                last_seen_at
            )
            SELECT
                :grid_cell_id,
                :region_id,
                :region_code,
                :grid_version,
                :acquisition_date,
                NULL,
                NULL,
                'S2MSI2A',
                :source_product_id,
                CAST(:metadata AS jsonb),
                :is_current,
                now()
            """
        ),
        {
            "grid_cell_id": cell_id,
            "region_id": region_id,
            "region_code": region_code,
            "grid_version": grid_version,
            "acquisition_date": fallback_date,
            "source_product_id": f"sentinel-hub-statistics-unavailable-{fallback_date.isoformat()}",
            "is_current": not history_mode,
            "metadata": _json(
                {
                    "pipeline": pipeline_name,
                    "region_id": region_id,
                    "region_code": region_code,
                    "grid_version": grid_version,
                    "cell_uuid": cell_uuid,
                    "product_name": None,
                    "catalogue_product_id": None,
                    "catalogue_match_type": "none",
                    "statistics_source": "sentinel_hub_statistical_api",
                    "interval": {},
                    "stats": {},
                    "geometry_pixel_count": None,
                    "sample_count": None,
                    "no_data_count": None,
                    "valid_pixel_count": 0,
                    "valid_pixel_ratio": 0,
                    "min_valid_pixel_ratio": min_valid_pixel_ratio,
                    "max_cloud_coverage": max_cloud_coverage,
                    "statistics_request": _statistics_request_metadata(
                        request_start=request_start,
                        request_end=request_end,
                        request_resolution=request_resolution,
                        max_cloud_coverage=max_cloud_coverage,
                        min_valid_pixel_ratio=min_valid_pixel_ratio,
                    ),
                    "bands": ["B04", "B08", "SCL", "dataMask"],
                    "status": "request_failed",
                    "error": error,
                }
            ),
        },
    )
    return 1


def store_unavailable_observations(
    db,
    cells: list[dict],
    region_code: str,
    grid_version: str,
    max_cloud_coverage: int,
    fallback_date: Date,
    error: str,
    min_valid_pixel_ratio: float = MIN_VALID_PIXEL_RATIO,
    history_mode: bool = False,
    pipeline_name: str = DEFAULT_PIPELINE_NAME,
) -> int:
    inserted = 0
    for cell in cells:
        inserted += store_unavailable_observation(
            db,
            cell_id=cell["id"],
            cell_uuid=cell["uuid"],
            region_code=region_code,
            grid_version=grid_version,
            max_cloud_coverage=max_cloud_coverage,
            fallback_date=fallback_date,
            error=error,
            min_valid_pixel_ratio=min_valid_pixel_ratio,
            history_mode=history_mode,
            pipeline_name=pipeline_name,
        )
    return inserted


def _retire_cell_observations(
    db,
    cell_id: int,
    region_code: str,
    grid_version: str,
    pipeline_name: str = DEFAULT_PIPELINE_NAME,
) -> None:
    db.execute(
        text(
            """
            UPDATE sentinel_observations
            SET is_current = false
            WHERE grid_cell_id = :grid_cell_id
              AND metadata->>'pipeline' = :pipeline_name
              AND region_code = :region_code
              AND grid_version = :grid_version
            """
        ),
        {
            "grid_cell_id": cell_id,
            "region_code": region_code,
            "grid_version": grid_version,
            "pipeline_name": pipeline_name,
        },
    )


def retire_current_observations(
    db,
    region_code: str,
    grid_version: str,
    pipeline_name: str = DEFAULT_PIPELINE_NAME,
) -> None:
    db.execute(
        text(
            """
            UPDATE sentinel_observations observation
            SET is_current = false
            FROM grid_cells cell
            JOIN regions region ON region.id = cell.region_id
            WHERE observation.grid_cell_id = cell.id
              AND observation.is_current
              AND observation.metadata->>'pipeline' = :pipeline_name
              AND observation.region_code = :region_code
              AND observation.grid_version = :grid_version
              AND region.code = :region_code
              AND cell.grid_version = :grid_version
            """
        ),
        {
            "region_code": region_code,
            "grid_version": grid_version,
            "pipeline_name": pipeline_name,
        },
    )


def retire_stage1_observations(db, region_code: str, grid_version: str) -> None:
    retire_current_observations(db, region_code, grid_version, DEFAULT_PIPELINE_NAME)


def _catalogue_match(
    product_by_date: dict[Date, SentinelProduct],
    products: list[SentinelProduct],
    target_date: Date,
) -> tuple[SentinelProduct | None, str]:
    product = product_by_date.get(target_date)
    if product is not None:
        return product, "same_day_catalogue_match"
    product = _nearest_product(products, target_date)
    if product is not None:
        return product, "nearest_catalogue_match"
    return None, "none"


def _nearest_product(products: list[SentinelProduct], target_date) -> SentinelProduct | None:
    if not products:
        return None
    return min(products, key=lambda product: abs((product.acquisition_date - target_date).days))


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def _statistics_request_metadata(
    request_start: datetime | None,
    request_end: datetime | None,
    request_resolution: float | None,
    max_cloud_coverage: int,
    min_valid_pixel_ratio: float,
) -> dict:
    return {
        "statistics_source": "sentinel_hub_statistical_api",
        "time_range": {
            "from": request_start.isoformat() if request_start else None,
            "to": request_end.isoformat() if request_end else None,
        },
        "resolution": request_resolution,
        "max_cloud_coverage": max_cloud_coverage,
        "min_valid_pixel_ratio": min_valid_pixel_ratio,
        "bands": ["B04", "B08", "SCL", "dataMask"],
    }


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _valid_pixel_count(sample_count: int | None, no_data_count: int | None, ndvi) -> int | None:
    if sample_count is None:
        return None
    if no_data_count is None:
        return sample_count if ndvi is not None else 0
    return max(0, sample_count - no_data_count)


def _valid_pixel_ratio(
    valid_pixel_count: int | None,
    sample_count: int | None,
    ndvi,
) -> float:
    if sample_count is None or sample_count <= 0:
        return 0.0
    if valid_pixel_count is None:
        return 0.0
    return round(valid_pixel_count / sample_count, 5)


def _ndvi_status(ndvi, valid_pixel_ratio: float, min_valid_pixel_ratio: float) -> str:
    if ndvi is None:
        return "no_ndvi"
    if valid_pixel_ratio < min_valid_pixel_ratio:
        return "no_valid_pixels"
    return "ok"


def _parse_day(value: str, end_of_day: bool = False) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    if end_of_day:
        return parsed.replace(hour=23, minute=59, second=59)
    return parsed


def _short_error(exc: Exception) -> str:
    message = str(exc).splitlines()
    detail = message[0] if message else "no details"
    return f"{type(exc).__name__}: {detail[:240]}"


def should_report_progress(index: int, total: int) -> bool:
    return index == 1 or index == total or index % 25 == 0


if __name__ == "__main__":
    main()
