from __future__ import annotations

import argparse
import json
import signal
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass

import requests
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.ogm import OgmBolmeClient, OgmFeature, OgmMescereWmsClient
from app.services.source_strategy import SOURCE_LAYER_BOLME, SOURCE_LAYER_MESCERE
from app.commands.ingest_ogm_test_subset import _insert_current_features, _record_source_check


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Layer A data by bbox tiles")
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--bbox", default="", help="min_lon,min_lat,max_lon,max_lat")
    parser.add_argument("--bbox-from-region", action="store_true")
    parser.add_argument("--tile-size", type=float, default=0.12)
    parser.add_argument("--source", choices=("all", "bolme", "mescere"), default="all")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--source-time-budget", type=int, default=0)
    parser.add_argument("--source-retries", type=int, default=3)
    parser.add_argument("--transport-retries", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--tile-delay", type=float, default=0.0)
    parser.add_argument("--retry-run-id", type=int, default=0)
    parser.add_argument(
        "--record-source-check-run-ids",
        default="",
        help="comma-separated completed shard/full run ids to aggregate into Layer A source_checks",
    )
    parser.add_argument("--subdivide-failed-tiles", type=int, default=0)
    parser.add_argument("--pre-subdivide-tiles", type=int, default=0)
    parser.add_argument(
        "--tile-intersect-source-layers",
        default="",
        help="comma-separated current source layers; fetch only bbox tiles intersecting any listed layer",
    )
    parser.add_argument("--max-failed-tiles", type=int, default=5)
    parser.add_argument("--max-tiles", type=int, default=0)
    parser.add_argument("--tile-shard-count", type=int, default=1)
    parser.add_argument("--tile-shard-index", type=int, default=0)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--retire-missing-bolme", action="store_true")
    parser.add_argument("--no-retire-missing-bolme", action="store_true")
    parser.add_argument("--retire-missing-mescere", action="store_true")
    parser.add_argument("--no-retire-missing-mescere", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.max_failed_tiles < 0:
        raise ValueError("max_failed_tiles must not be negative")
    if args.max_tiles < 0:
        raise ValueError("max_tiles must not be negative")
    if args.tile_shard_count < 1:
        raise ValueError("tile_shard_count must be at least 1")
    if args.tile_shard_index < 0 or args.tile_shard_index >= args.tile_shard_count:
        raise ValueError("tile_shard_index must be between 0 and tile_shard_count - 1")
    if args.source_time_budget < 0:
        raise ValueError("source_time_budget must not be negative")
    if args.source_retries < 1:
        raise ValueError("source_retries must be at least 1")
    if args.transport_retries < 0:
        raise ValueError("transport_retries must not be negative")
    if args.retry_delay < 0:
        raise ValueError("retry_delay must not be negative")
    if args.tile_delay < 0:
        raise ValueError("tile_delay must not be negative")
    if args.subdivide_failed_tiles < 0:
        raise ValueError("subdivide_failed_tiles must not be negative")
    if args.pre_subdivide_tiles < 0:
        raise ValueError("pre_subdivide_tiles must not be negative")
    if args.retry_run_id < 0:
        raise ValueError("retry_run_id must not be negative")
    if args.retry_run_id and (args.plan_only or args.dry_run or args.max_tiles):
        raise ValueError("retry_run_id cannot be combined with --plan-only, --dry-run, or --max-tiles")
    if args.record_source_check_run_ids and (
        args.retry_run_id or args.plan_only or args.dry_run or args.max_tiles
    ):
        raise ValueError(
            "record_source_check_run_ids cannot be combined with "
            "--retry-run-id, --plan-only, --dry-run, or --max-tiles"
        )
    if args.retry_run_id and (args.tile_shard_count != 1 or args.tile_shard_index != 0):
        raise ValueError("retry_run_id uses the tile shard recorded on the original run")
    if args.retry_run_id and args.tile_intersect_source_layers:
        raise ValueError("retry_run_id uses the source-layer tile filter recorded on the original run")
    tile_filter_source_layers = _parse_source_layers(args.tile_intersect_source_layers)
    if tile_filter_source_layers and (
        (args.retire_missing_bolme and not args.no_retire_missing_bolme)
        or (args.retire_missing_mescere and not args.no_retire_missing_mescere)
    ):
        raise ValueError("retire_missing cannot be combined with --tile-intersect-source-layers")
    if args.retire_missing_bolme and not args.no_retire_missing_bolme:
        if args.max_tiles or not args.bbox_from_region:
            raise ValueError("retire_missing_bolme requires --bbox-from-region and no --max-tiles")
    if args.retire_missing_mescere and not args.no_retire_missing_mescere:
        if args.max_tiles or not args.bbox_from_region:
            raise ValueError("retire_missing_mescere requires --bbox-from-region and no --max-tiles")

    settings = get_settings()
    fetched_totals = {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0}
    inserted_totals = {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0}
    failures = {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0}
    seen_ids = {SOURCE_LAYER_BOLME: set(), SOURCE_LAYER_MESCERE: set()}
    checked_tiles = 0
    run_pk: int | None = None
    finished_status = "completed"
    lock_key = _lock_key(args)

    with SessionLocal() as db:
        lock_acquired = db.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:lock_key))"),
            {"lock_key": lock_key},
        ).scalar_one()
        if not lock_acquired:
            raise RuntimeError("Stage 3 Layer A ingest is already running")

        try:
            region = db.execute(
                text("SELECT id, code FROM regions WHERE code = :region_code"),
                {"region_code": args.region_code},
            ).mappings().one()
            if args.record_source_check_run_ids:
                aggregate = _record_source_check_from_completed_runs(db, args, region)
                db.commit()
                print(
                    "stage3 layer-a source-check recorded "
                    f"region={args.region_code} source={aggregate['source']} "
                    f"runs={aggregate['run_ids']} total_tiles={aggregate['total_tiles']} "
                    f"processed_tiles={aggregate['processed_tiles']} "
                    f"bolme_fetched={aggregate['fetched'][SOURCE_LAYER_BOLME]} "
                    f"mescere_fetched={aggregate['fetched'][SOURCE_LAYER_MESCERE]}"
                )
                return
            if args.retry_run_id:
                retry_result = _retry_failed_run(db, settings, args, region)
                db.commit()
                print(
                    "stage3 layer-a retry finished "
                    f"region={args.region_code} run_id={args.retry_run_id} "
                    f"retried_tiles={retry_result['retried_tiles']} "
                    f"remaining_failed_tiles={retry_result['remaining_failed_tiles']} "
                    f"status={retry_result['status']}"
                )
                if retry_result["status"] != "completed":
                    raise SystemExit(1)
                return
            bbox = _load_region_bbox(db, args.region_code) if args.bbox_from_region else _parse_bbox(args.bbox)
            tile_work = _tile_work_items(
                db,
                bbox=bbox,
                tile_size=args.tile_size,
                max_tiles=args.max_tiles,
                region_code=region["code"],
                intersect_source_layers=tile_filter_source_layers,
                tile_shard_count=args.tile_shard_count,
                tile_shard_index=args.tile_shard_index,
            )
            if args.plan_only:
                print(
                    "stage3 layer-a ingest plan "
                    f"region={args.region_code} source={args.source} tiles={len(tile_work)} "
                    f"tile_size={args.tile_size} max_tiles={args.max_tiles or 'all'} "
                    f"tile_shard={args.tile_shard_index}/{args.tile_shard_count} "
                    f"tile_intersect_source_layers={args.tile_intersect_source_layers or 'none'} "
                    f"bbox={','.join(str(round(value, 6)) for value in bbox)}"
                )
                return
            if not tile_work:
                raise RuntimeError("Layer A ingest has no bbox tiles after source-layer intersection filter")
            track_seen_features = _should_track_seen_features(args)
            if not args.dry_run:
                if track_seen_features:
                    _prepare_seen_table(db)
                run_pk = _create_ingest_run(db, region["id"], args, bbox, total_tiles=len(tile_work))
                db.commit()

            for tile in tile_work:
                checked_tiles += 1
                tile_bbox = tile["bbox"]
                tile_fetched = {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0}
                tile_inserted = {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0}
                tile_failures = {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0}
                tile_errors = {SOURCE_LAYER_BOLME: "", SOURCE_LAYER_MESCERE: ""}
                if args.source in ("all", "bolme"):
                    try:
                        with _source_deadline(_source_time_budget(args), SOURCE_LAYER_BOLME):
                            result = _fetch_bolme(
                                settings,
                                tile_bbox,
                                args.timeout,
                                db,
                                region["code"],
                                retries=args.source_retries,
                                transport_retries=args.transport_retries,
                                retry_delay=args.retry_delay,
                                subdivide_failed_tiles=args.subdivide_failed_tiles,
                                pre_subdivide_tiles=args.pre_subdivide_tiles,
                            )
                    except TimeoutError as exc:
                        result = FetchResult(ok=False, features=[], error=_short_error(exc))
                    failures[SOURCE_LAYER_BOLME] += 0 if result.ok else 1
                    tile_failures[SOURCE_LAYER_BOLME] += 0 if result.ok else 1
                    tile_errors[SOURCE_LAYER_BOLME] = result.error or ""
                    if track_seen_features and not args.dry_run:
                        _record_seen_features(db, SOURCE_LAYER_BOLME, result.features)
                    features = _new_features(result.features, seen_ids[SOURCE_LAYER_BOLME])
                    fetched_totals[SOURCE_LAYER_BOLME] += len(features)
                    tile_fetched[SOURCE_LAYER_BOLME] += len(features)
                    if features and not args.dry_run:
                        inserted_count = _insert_current_features(
                            db,
                            region,
                            features,
                            source_layer=SOURCE_LAYER_BOLME,
                            source_url=settings.ogm_bolme_feature_url,
                            pipeline="stage3_ogm_bolme",
                            official_status="official_bolme",
                            timeout=args.timeout,
                            tls_verify=settings.ogm_tls_verify,
                            retire_existing=False,
                            replace_matching=True,
                            clip_to_region=True,
                        )
                        inserted_totals[SOURCE_LAYER_BOLME] += inserted_count
                        tile_inserted[SOURCE_LAYER_BOLME] += inserted_count
                if args.source in ("all", "mescere"):
                    try:
                        with _source_deadline(_source_time_budget(args), SOURCE_LAYER_MESCERE):
                            result = _fetch_mescere(
                                settings,
                                tile_bbox,
                                args.timeout,
                                db,
                                region["code"],
                                retries=args.source_retries,
                                transport_retries=args.transport_retries,
                                retry_delay=args.retry_delay,
                                subdivide_failed_tiles=args.subdivide_failed_tiles,
                                pre_subdivide_tiles=args.pre_subdivide_tiles,
                            )
                    except TimeoutError as exc:
                        result = FetchResult(ok=False, features=[], error=_short_error(exc))
                    failures[SOURCE_LAYER_MESCERE] += 0 if result.ok else 1
                    tile_failures[SOURCE_LAYER_MESCERE] += 0 if result.ok else 1
                    tile_errors[SOURCE_LAYER_MESCERE] = result.error or ""
                    if track_seen_features and not args.dry_run:
                        _record_seen_features(db, SOURCE_LAYER_MESCERE, result.features)
                    features = _new_features(result.features, seen_ids[SOURCE_LAYER_MESCERE])
                    fetched_totals[SOURCE_LAYER_MESCERE] += len(features)
                    tile_fetched[SOURCE_LAYER_MESCERE] += len(features)
                    if features and not args.dry_run:
                        inserted_count = _insert_current_features(
                            db,
                            region,
                            features,
                            source_layer=SOURCE_LAYER_MESCERE,
                            source_url=settings.ogm_mescere_wms_url,
                            pipeline="stage3_ogm_mescere",
                            official_status="public_mescere_wms_derived",
                            timeout=args.timeout,
                            tls_verify=settings.ogm_tls_verify,
                            retire_existing=False,
                            replace_matching=True,
                            clip_to_region=True,
                        )
                        inserted_totals[SOURCE_LAYER_MESCERE] += inserted_count
                        tile_inserted[SOURCE_LAYER_MESCERE] += inserted_count
                if run_pk is not None:
                    _record_ingest_tile(
                        db,
                        run_pk=run_pk,
                        tile_bbox=tile_bbox,
                        tile_index=int(tile["tile_key"]),
                        fetched=tile_fetched,
                        inserted=tile_inserted,
                        failures=tile_failures,
                        errors=tile_errors,
                    )
                    _update_ingest_run(
                        db,
                        run_pk,
                        checked_tiles,
                        fetched_totals,
                        inserted_totals,
                        failures,
                        status="running",
                    )
                if not args.dry_run:
                    db.commit()
                if sum(failures.values()) > args.max_failed_tiles:
                    if run_pk is not None:
                        _update_ingest_run(
                            db,
                            run_pk,
                            checked_tiles,
                            fetched_totals,
                            inserted_totals,
                            failures,
                            status="failed",
                            error="Layer A ingest stopped after repeated source failures",
                        )
                        db.commit()
                    raise RuntimeError("Layer A ingest stopped after repeated source failures")
                if args.tile_delay and checked_tiles < len(tile_work):
                    time.sleep(args.tile_delay)
            if not args.dry_run:
                if (
                    args.source in ("all", "bolme")
                    and args.retire_missing_bolme
                    and not args.no_retire_missing_bolme
                    and failures[SOURCE_LAYER_BOLME] == 0
                ):
                    _retire_missing_current_features(db, SOURCE_LAYER_BOLME, region["code"])
                if (
                    args.source in ("all", "mescere")
                    and args.retire_missing_mescere
                    and not args.no_retire_missing_mescere
                    and failures[SOURCE_LAYER_MESCERE] == 0
                ):
                    _retire_missing_current_features(db, SOURCE_LAYER_MESCERE, region["code"])
                if run_pk is not None:
                    final_status = _final_run_status(failures)
                    finished_status = final_status
                    if _should_record_aggregate_source_checks(args):
                        _record_aggregate_source_checks(
                            db,
                            args=args,
                            region_code=region["code"],
                            fetched=fetched_totals,
                            failures=failures,
                        )
                    _update_ingest_run(
                        db,
                        run_pk,
                        checked_tiles,
                        fetched_totals,
                        inserted_totals,
                        failures,
                        status=final_status,
                        error=None if final_status == "completed" else "Layer A ingest finished with failed tiles",
                    )
                db.commit()
        except Exception as exc:
            if run_pk is not None and not args.dry_run:
                db.rollback()
                _mark_ingest_run_failed(
                    run_pk,
                    checked_tiles,
                    fetched_totals,
                    inserted_totals,
                    failures,
                    _short_error(exc),
                )
            raise
        finally:
            db.rollback()
            db.execute(
                text("SELECT pg_advisory_unlock(hashtext(:lock_key))"),
                {"lock_key": lock_key},
            )
            db.commit()

    print(
        "stage3 layer-a ingest finished "
        f"region={args.region_code} tiles={checked_tiles} "
        f"bolme_fetched={fetched_totals[SOURCE_LAYER_BOLME]} "
        f"mescere_fetched={fetched_totals[SOURCE_LAYER_MESCERE]} "
        f"bolme_inserted={inserted_totals[SOURCE_LAYER_BOLME]} "
        f"mescere_inserted={inserted_totals[SOURCE_LAYER_MESCERE]} "
        f"failed_bolme={failures[SOURCE_LAYER_BOLME]} failed_mescere={failures[SOURCE_LAYER_MESCERE]} "
        f"dry_run={str(args.dry_run).lower()}"
    )
    if not args.dry_run and finished_status != "completed":
        raise SystemExit("Layer A ingest finished with failed tiles")


@dataclass(frozen=True)
class FetchResult:
    ok: bool
    features: list[OgmFeature]
    error: str | None = None


def _fetch_bolme(
    settings,
    bbox,
    timeout: int,
    db,
    region_code: str,
    *,
    retries: int,
    transport_retries: int,
    retry_delay: float,
    subdivide_failed_tiles: int,
    pre_subdivide_tiles: int,
) -> FetchResult:
    client = OgmBolmeClient(
        settings.ogm_bolme_feature_url,
        timeout=timeout,
        verify_tls=settings.ogm_tls_verify,
        proxy_url=settings.ogm_proxy_url,
        transport_retries=transport_retries,
    )
    return _fetch_bbox_with_subdivision(
        source_layer=SOURCE_LAYER_BOLME,
        fetch_bbox=client.query_bbox,
        bbox=bbox,
        retries=retries,
        retry_delay=retry_delay,
        remaining_depth=subdivide_failed_tiles,
        pre_subdivide_depth=pre_subdivide_tiles,
    )


def _fetch_mescere(
    settings,
    bbox,
    timeout: int,
    db,
    region_code: str,
    *,
    retries: int,
    transport_retries: int,
    retry_delay: float,
    subdivide_failed_tiles: int,
    pre_subdivide_tiles: int,
) -> FetchResult:
    client = OgmMescereWmsClient(
        settings.ogm_mescere_wms_url,
        timeout=timeout,
        verify_tls=settings.ogm_tls_verify,
        proxy_url=settings.ogm_proxy_url,
        transport_retries=transport_retries,
    )
    return _fetch_bbox_with_subdivision(
        source_layer=SOURCE_LAYER_MESCERE,
        fetch_bbox=client.query_bbox,
        bbox=bbox,
        retries=retries,
        retry_delay=retry_delay,
        remaining_depth=subdivide_failed_tiles,
        pre_subdivide_depth=pre_subdivide_tiles,
    )


def _fetch_bbox_with_subdivision(
    *,
    source_layer: str,
    fetch_bbox,
    bbox: tuple[float, float, float, float],
    retries: int,
    retry_delay: float,
    remaining_depth: int,
    pre_subdivide_depth: int = 0,
) -> FetchResult:
    if pre_subdivide_depth > 0:
        return _fetch_split_bboxes(
            source_layer=source_layer,
            fetch_bbox=fetch_bbox,
            bbox=bbox,
            retries=retries,
            retry_delay=retry_delay,
            remaining_depth=remaining_depth,
            pre_subdivide_depth=pre_subdivide_depth - 1,
        )

    result = _fetch_with_retries(
        source_layer=source_layer,
        fetch=lambda: fetch_bbox(bbox),
        retries=retries,
        retry_delay=retry_delay,
    )
    if result.ok or remaining_depth <= 0:
        return result
    if not _should_subdivide_source_error(result.error or ""):
        return result

    print(
        "stage3 layer-a source subdivide "
        f"source={source_layer} remaining_depth={remaining_depth} "
        f"bbox={','.join(str(round(value, 8)) for value in bbox)} "
        f"error={result.error}",
        flush=True,
    )
    return _fetch_split_bboxes(
        source_layer=source_layer,
        fetch_bbox=fetch_bbox,
        bbox=bbox,
        retries=retries,
        retry_delay=retry_delay,
        remaining_depth=remaining_depth - 1,
        pre_subdivide_depth=0,
    )


def _fetch_split_bboxes(
    *,
    source_layer: str,
    fetch_bbox,
    bbox: tuple[float, float, float, float],
    retries: int,
    retry_delay: float,
    remaining_depth: int,
    pre_subdivide_depth: int,
) -> FetchResult:
    features: list[OgmFeature] = []
    errors: list[str] = []
    for child_bbox in _split_bbox(bbox):
        child_result = _fetch_bbox_with_subdivision(
            source_layer=source_layer,
            fetch_bbox=fetch_bbox,
            bbox=child_bbox,
            retries=retries,
            retry_delay=retry_delay,
            remaining_depth=remaining_depth,
            pre_subdivide_depth=pre_subdivide_depth,
        )
        if child_result.ok:
            features.extend(child_result.features)
        else:
            errors.append(child_result.error or "unknown child bbox error")
    if errors:
        return FetchResult(ok=False, features=[], error="; ".join(errors)[:500])
    return FetchResult(ok=True, features=features)


def _should_subdivide_source_error(error: str) -> bool:
    lowered = error.lower()
    if any(
        marker in lowered
        for marker in (
            "connectionerror",
            "connecttimeout",
            "readtimeout",
            "sslerror",
            "httpsconnectionpool",
            "max retries exceeded",
            "temporarily unavailable",
        )
    ):
        return False
    return any(
        marker in lowered
        for marker in (
            "response too large",
            "transfer limit",
            "pagination",
            "duplicate page",
            "split the bbox",
            "too many",
            "paths",
        )
    )


def _split_bbox(bbox: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lon = (min_lon + max_lon) / 2
    mid_lat = (min_lat + max_lat) / 2
    return [
        (min_lon, min_lat, mid_lon, mid_lat),
        (mid_lon, min_lat, max_lon, mid_lat),
        (min_lon, mid_lat, mid_lon, max_lat),
        (mid_lon, mid_lat, max_lon, max_lat),
    ]


def _fetch_with_retries(source_layer: str, fetch, retries: int, retry_delay: float) -> FetchResult:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return FetchResult(ok=True, features=fetch())
        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            print(
                "stage3 layer-a source retry "
                f"source={source_layer} attempt={attempt + 1}/{retries} "
                f"error={_short_error(exc)}",
                flush=True,
            )
            if retry_delay:
                time.sleep(retry_delay)
    if last_error is None:
        return FetchResult(ok=False, features=[], error="unknown source error")
    return FetchResult(ok=False, features=[], error=_short_error(last_error))


def _retry_failed_run(db, settings, args, region: dict) -> dict[str, int | str]:
    run = db.execute(
        text(
            """
            SELECT id, source, status, tile_size, total_tiles, bbox, metadata
            FROM stage3_layer_a_ingest_runs
            WHERE id = :run_id
              AND region_id = :region_id
            """
        ),
        {"run_id": args.retry_run_id, "region_id": region["id"]},
    ).mappings().one_or_none()
    if not run:
        raise RuntimeError(f"Layer A ingest run not found for retry: {args.retry_run_id}")
    if run["status"] == "running":
        raise RuntimeError("cannot retry failed tiles while the original Layer A ingest run is still running")

    metadata = _metadata_from_payload(run.get("metadata"))
    source = run["source"] or args.source
    if source not in {"all", "bolme", "mescere"}:
        raise RuntimeError(f"unsupported Layer A source in retry run: {source}")
    args.source = source

    retry_tiles = _retry_or_missing_tiles(db, run, args)

    retried_tiles = 0
    seen_ids = {SOURCE_LAYER_BOLME: set(), SOURCE_LAYER_MESCERE: set()}
    for tile in retry_tiles:
        tile_bbox = (
            float(tile["min_lon"]),
            float(tile["min_lat"]),
            float(tile["max_lon"]),
            float(tile["max_lat"]),
        )
        tile_fetched = {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0}
        tile_inserted = {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0}
        tile_failures = {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0}
        tile_errors = {SOURCE_LAYER_BOLME: "", SOURCE_LAYER_MESCERE: ""}

        if source in ("all", "bolme"):
            try:
                with _source_deadline(_source_time_budget(args), SOURCE_LAYER_BOLME):
                    result = _fetch_bolme(
                        settings,
                        tile_bbox,
                        args.timeout,
                        db,
                        region["code"],
                        retries=args.source_retries,
                        transport_retries=args.transport_retries,
                        retry_delay=args.retry_delay,
                        subdivide_failed_tiles=args.subdivide_failed_tiles,
                        pre_subdivide_tiles=args.pre_subdivide_tiles,
                    )
            except TimeoutError as exc:
                result = FetchResult(ok=False, features=[], error=_short_error(exc))
            tile_failures[SOURCE_LAYER_BOLME] += 0 if result.ok else 1
            tile_errors[SOURCE_LAYER_BOLME] = result.error or ""
            features = _new_features(result.features, seen_ids[SOURCE_LAYER_BOLME])
            tile_fetched[SOURCE_LAYER_BOLME] += len(features)
            if features:
                tile_inserted[SOURCE_LAYER_BOLME] += _insert_current_features(
                    db,
                    region,
                    features,
                    source_layer=SOURCE_LAYER_BOLME,
                    source_url=settings.ogm_bolme_feature_url,
                    pipeline="stage3_ogm_bolme",
                    official_status="official_bolme",
                    timeout=args.timeout,
                    tls_verify=settings.ogm_tls_verify,
                    retire_existing=False,
                    replace_matching=True,
                    clip_to_region=True,
                )

        if source in ("all", "mescere"):
            try:
                with _source_deadline(_source_time_budget(args), SOURCE_LAYER_MESCERE):
                    result = _fetch_mescere(
                        settings,
                        tile_bbox,
                        args.timeout,
                        db,
                        region["code"],
                        retries=args.source_retries,
                        transport_retries=args.transport_retries,
                        retry_delay=args.retry_delay,
                        subdivide_failed_tiles=args.subdivide_failed_tiles,
                        pre_subdivide_tiles=args.pre_subdivide_tiles,
                    )
            except TimeoutError as exc:
                result = FetchResult(ok=False, features=[], error=_short_error(exc))
            tile_failures[SOURCE_LAYER_MESCERE] += 0 if result.ok else 1
            tile_errors[SOURCE_LAYER_MESCERE] = result.error or ""
            features = _new_features(result.features, seen_ids[SOURCE_LAYER_MESCERE])
            tile_fetched[SOURCE_LAYER_MESCERE] += len(features)
            if features:
                tile_inserted[SOURCE_LAYER_MESCERE] += _insert_current_features(
                    db,
                    region,
                    features,
                    source_layer=SOURCE_LAYER_MESCERE,
                    source_url=settings.ogm_mescere_wms_url,
                    pipeline="stage3_ogm_mescere",
                    official_status="public_mescere_wms_derived",
                    timeout=args.timeout,
                    tls_verify=settings.ogm_tls_verify,
                    retire_existing=False,
                    replace_matching=True,
                    clip_to_region=True,
                )

        _record_ingest_tile(
            db,
            run_pk=args.retry_run_id,
            tile_bbox=tile_bbox,
            tile_index=int(tile["tile_key"]),
            fetched=tile_fetched,
            inserted=tile_inserted,
            failures=tile_failures,
            errors=tile_errors,
        )
        _refresh_retry_run_progress(db, args.retry_run_id, status="running")
        db.commit()
        retried_tiles += 1
        persisted = _ingest_tile_totals(db, args.retry_run_id)
        if _failure_total(persisted) > args.max_failed_tiles:
            failures = {
                SOURCE_LAYER_BOLME: persisted["failed_bolme"],
                SOURCE_LAYER_MESCERE: persisted["failed_mescere"],
            }
            fetched = {
                SOURCE_LAYER_BOLME: persisted["bolme_fetched"],
                SOURCE_LAYER_MESCERE: persisted["mescere_fetched"],
            }
            inserted = {
                SOURCE_LAYER_BOLME: persisted["bolme_inserted"],
                SOURCE_LAYER_MESCERE: persisted["mescere_inserted"],
            }
            _update_ingest_run(
                db,
                args.retry_run_id,
                persisted["processed_tiles"],
                fetched,
                inserted,
                failures,
                status="failed",
                error="Layer A retry/resume stopped after repeated source failures",
            )
            db.commit()
            raise RuntimeError("Layer A retry/resume stopped after repeated source failures")
        if args.tile_delay and retried_tiles < len(retry_tiles):
            time.sleep(args.tile_delay)

    persisted = _ingest_tile_totals(db, args.retry_run_id)
    remaining_failed_tiles = _remaining_failed_tiles(db, args.retry_run_id)
    status = (
        "completed"
        if remaining_failed_tiles == 0 and persisted["processed_tiles"] >= int(run["total_tiles"] or 0)
        else "failed"
    )
    failures = {
        SOURCE_LAYER_BOLME: persisted["failed_bolme"],
        SOURCE_LAYER_MESCERE: persisted["failed_mescere"],
    }
    fetched = {
        SOURCE_LAYER_BOLME: persisted["bolme_fetched"],
        SOURCE_LAYER_MESCERE: persisted["mescere_fetched"],
    }
    inserted = {
        SOURCE_LAYER_BOLME: persisted["bolme_inserted"],
        SOURCE_LAYER_MESCERE: persisted["mescere_inserted"],
    }
    _update_ingest_run(
        db,
        args.retry_run_id,
        persisted["processed_tiles"],
        fetched,
        inserted,
        failures,
        status=status,
        error=None if status == "completed" else "Layer A retry/resume finished with missing or failed tiles",
    )
    if status == "completed" and _should_record_aggregate_source_checks_for_metadata(metadata):
        _record_aggregate_source_checks(db, args=args, region_code=region["code"], fetched=fetched, failures=failures)
    return {
        "retried_tiles": retried_tiles,
        "remaining_failed_tiles": remaining_failed_tiles,
        "status": status,
    }


def _retry_or_missing_tiles(db, run: dict, args) -> list[dict[str, float | str]]:
    existing_rows = db.execute(
        text(
            """
            SELECT
                tile_key,
                min_lon,
                min_lat,
                max_lon,
                max_lat,
                status,
                failed_bolme,
                failed_mescere
            FROM stage3_layer_a_ingest_tiles
            WHERE run_id = :run_id
            """
        ),
        {"run_id": args.retry_run_id},
    ).mappings().all()
    existing = {int(row["tile_key"]): row for row in existing_rows}
    metadata = _metadata_from_payload(run.get("metadata"))
    bbox = _bbox_from_payload(run["bbox"])
    tile_size = float(run["tile_size"] or args.tile_size)
    total_tiles = int(run["total_tiles"] or 0)
    candidates = _tile_work_items(
        db,
        bbox=bbox,
        tile_size=tile_size,
        max_tiles=0,
        region_code=args.region_code,
        intersect_source_layers=list(metadata.get("tile_intersect_source_layers") or []),
        tile_shard_count=int(metadata.get("tile_shard_count") or 1),
        tile_shard_index=int(metadata.get("tile_shard_index") or 0),
    )
    retry_tiles: list[dict[str, float | str]] = []
    for position, candidate in enumerate(candidates, start=1):
        if total_tiles and position > total_tiles:
            break
        tile_index = int(candidate["tile_key"])
        tile_bbox = candidate["bbox"]
        row = existing.get(tile_index)
        completed = (
            row is not None
            and row["status"] == "completed"
            and int(row["failed_bolme"] or 0) == 0
            and int(row["failed_mescere"] or 0) == 0
        )
        if completed:
            continue
        if row is not None:
            retry_tiles.append(
                {
                    "tile_key": str(tile_index),
                    "min_lon": float(row["min_lon"]),
                    "min_lat": float(row["min_lat"]),
                    "max_lon": float(row["max_lon"]),
                    "max_lat": float(row["max_lat"]),
                }
            )
        else:
            retry_tiles.append(
                {
                    "tile_key": str(tile_index),
                    "min_lon": tile_bbox[0],
                    "min_lat": tile_bbox[1],
                    "max_lon": tile_bbox[2],
                    "max_lat": tile_bbox[3],
                }
            )
    return retry_tiles

def _refresh_retry_run_progress(db, run_pk: int, *, status: str) -> None:
    persisted = _ingest_tile_totals(db, run_pk)
    _update_ingest_run(
        db,
        run_pk,
        persisted["processed_tiles"],
        {
            SOURCE_LAYER_BOLME: persisted["bolme_fetched"],
            SOURCE_LAYER_MESCERE: persisted["mescere_fetched"],
        },
        {
            SOURCE_LAYER_BOLME: persisted["bolme_inserted"],
            SOURCE_LAYER_MESCERE: persisted["mescere_inserted"],
        },
        {
            SOURCE_LAYER_BOLME: persisted["failed_bolme"],
            SOURCE_LAYER_MESCERE: persisted["failed_mescere"],
        },
        status=status,
    )


def _failure_total(values: dict[str, int]) -> int:
    return int(values.get("failed_bolme") or 0) + int(values.get("failed_mescere") or 0)


def _remaining_failed_tiles(db, run_pk: int) -> int:
    return int(
        db.execute(
            text(
                """
                SELECT count(*)
                FROM stage3_layer_a_ingest_tiles
                WHERE run_id = :run_id
                  AND (
                      status = 'failed'
                      OR failed_bolme > 0
                      OR failed_mescere > 0
                  )
                """
            ),
            {"run_id": run_pk},
        ).scalar_one()
    )


def _source_time_budget(args) -> int:
    if args.source_time_budget:
        return args.source_time_budget
    return max(args.timeout * 4, args.timeout + 10)


def _lock_key(args) -> str:
    if args.retry_run_id:
        return f"stage3_layer_a_ingest_retry:{args.retry_run_id}"
    return (
        "stage3_layer_a_ingest:"
        f"{args.region_code}:{args.source}:{args.tile_shard_index}:{args.tile_shard_count}"
    )


def _should_record_aggregate_source_checks(args) -> bool:
    return args.max_tiles == 0 and args.tile_shard_count == 1


def _should_record_aggregate_source_checks_for_metadata(metadata: dict) -> bool:
    return int(metadata.get("max_tiles") or 0) == 0 and int(metadata.get("tile_shard_count") or 1) == 1


def _should_track_seen_features(args) -> bool:
    return bool(
        (args.source in ("all", "bolme") and args.retire_missing_bolme and not args.no_retire_missing_bolme)
        or (args.source in ("all", "mescere") and args.retire_missing_mescere and not args.no_retire_missing_mescere)
    )


@contextmanager
def _source_deadline(seconds: int, source_layer: str):
    if seconds <= 0:
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)

    def _raise_timeout(_signum, _frame):
        raise TimeoutError(f"{source_layer} query exceeded {seconds}s source time budget")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def _record_aggregate_source_checks(
    db,
    *,
    args,
    region_code: str,
    fetched: dict[str, int],
    failures: dict[str, int],
) -> None:
    settings = get_settings()
    if args.source in ("all", "bolme"):
        _record_source_check(
            db,
            source_layer=SOURCE_LAYER_BOLME,
            source_type="public_ogm_rest",
            region_code=region_code,
            status=_aggregate_source_status(fetched[SOURCE_LAYER_BOLME], failures[SOURCE_LAYER_BOLME]),
            feature_count=fetched[SOURCE_LAYER_BOLME],
            error=None if failures[SOURCE_LAYER_BOLME] == 0 else "one or more Layer A bbox tiles failed",
            timeout=args.timeout,
            tls_verify=settings.ogm_tls_verify,
            pipeline="stage3_layer_a_source_check",
        )
    if args.source in ("all", "mescere"):
        _record_source_check(
            db,
            source_layer=SOURCE_LAYER_MESCERE,
            source_type="public_wms_feature_info",
            region_code=region_code,
            status=_aggregate_source_status(fetched[SOURCE_LAYER_MESCERE], failures[SOURCE_LAYER_MESCERE]),
            feature_count=fetched[SOURCE_LAYER_MESCERE],
            error=None if failures[SOURCE_LAYER_MESCERE] == 0 else "one or more Layer A bbox tiles failed",
            timeout=args.timeout,
            tls_verify=settings.ogm_tls_verify,
            pipeline="stage3_layer_a_source_check",
        )


def _aggregate_source_status(feature_count: int, failure_count: int) -> str:
    if failure_count:
        return "unavailable"
    return "ok" if feature_count else "empty"


def _prepare_seen_table(db) -> None:
    db.execute(
        text(
            """
            CREATE TEMP TABLE IF NOT EXISTS stage3_layer_a_seen_source_ids (
                source_layer TEXT NOT NULL,
                source_id TEXT NOT NULL,
                PRIMARY KEY (source_layer, source_id)
            ) ON COMMIT PRESERVE ROWS
            """
        )
    )


def _record_seen_features(db, source_layer: str, features: list[OgmFeature]) -> None:
    rows = [
        {"source_layer": source_layer, "source_id": str(feature.source_id)}
        for feature in features
        if feature.source_id
    ]
    if not rows:
        return
    db.execute(
        text(
            """
            INSERT INTO stage3_layer_a_seen_source_ids (source_layer, source_id)
            VALUES (:source_layer, :source_id)
            ON CONFLICT DO NOTHING
            """
        ),
        rows,
    )


def _retire_missing_current_features(db, source_layer: str, region_code: str) -> int:
    seen_count = db.execute(
        text(
            """
            SELECT count(*)
            FROM stage3_layer_a_seen_source_ids
            WHERE source_layer = :source_layer
            """
        ),
        {"source_layer": source_layer},
    ).scalar_one()
    if not seen_count:
        return 0
    return db.execute(
        text(
            """
            UPDATE ogm_parcels parcel
            SET is_current = false
            WHERE parcel.is_current
              AND parcel.source_layer = :source_layer
              AND parcel.attributes->>'region_code' = :region_code
              AND NOT EXISTS (
                  SELECT 1
                  FROM stage3_layer_a_seen_source_ids seen
                  WHERE seen.source_layer = parcel.source_layer
                    AND seen.source_id = parcel.source_id
              )
            """
        ),
        {"source_layer": source_layer, "region_code": region_code},
    ).rowcount


def _create_ingest_run(
    db,
    region_id: int,
    args,
    bbox: tuple[float, float, float, float],
    *,
    total_tiles: int | None = None,
) -> int:
    if total_tiles is None:
        total_tiles = _tile_count(bbox, args.tile_size, args.max_tiles)
    region_geometry_hash = _region_geometry_hash(db, region_id)
    return db.execute(
        text(
            """
            INSERT INTO stage3_layer_a_ingest_runs (
                region_id,
                source,
                tile_size,
                total_tiles,
                bbox,
                metadata
            )
            VALUES (
                :region_id,
                :source,
                :tile_size,
                :total_tiles,
                CAST(:bbox AS jsonb),
                CAST(:metadata AS jsonb)
            )
            RETURNING id
            """
        ),
        {
            "region_id": region_id,
            "source": args.source,
            "tile_size": args.tile_size,
            "total_tiles": total_tiles,
            "bbox": json.dumps(_bbox_payload(bbox), separators=(",", ":")),
            "metadata": json.dumps(
                {
                    "max_tiles": args.max_tiles or None,
                    "max_failed_tiles": args.max_failed_tiles,
                    "source_retries": args.source_retries,
                    "transport_retries": args.transport_retries,
                    "retry_delay": args.retry_delay,
                    "tile_delay": args.tile_delay,
                    "subdivide_failed_tiles": args.subdivide_failed_tiles,
                    "pre_subdivide_tiles": args.pre_subdivide_tiles,
                    "tile_intersect_source_layers": _parse_source_layers(args.tile_intersect_source_layers),
                    "tile_shard_count": args.tile_shard_count,
                    "tile_shard_index": args.tile_shard_index,
                    "bbox_source": "region" if args.bbox_from_region else "argument",
                    "region_geometry_hash": region_geometry_hash,
                    "commit_policy": "per_tile",
                    "retire_missing_bolme": bool(args.retire_missing_bolme and not args.no_retire_missing_bolme),
                    "retire_missing_mescere": bool(
                        args.retire_missing_mescere and not args.no_retire_missing_mescere
                    ),
                },
                separators=(",", ":"),
            ),
        },
    ).scalar_one()


def _region_geometry_hash(db, region_id: int) -> str:
    return db.execute(
        text(
            """
            SELECT encode(digest(ST_AsEWKB(geom), 'sha256'), 'hex')
            FROM regions
            WHERE id = :region_id
            """
        ),
        {"region_id": region_id},
    ).scalar_one()


def _record_ingest_tile(
    db,
    *,
    run_pk: int,
    tile_bbox: tuple[float, float, float, float],
    tile_index: int,
    fetched: dict[str, int],
    inserted: dict[str, int],
    failures: dict[str, int],
    errors: dict[str, str] | None = None,
) -> None:
    failed = bool(sum(failures.values()))
    error = _tile_error_text(errors or {})
    db.execute(
        text(
            """
            INSERT INTO stage3_layer_a_ingest_tiles (
                run_id,
                tile_key,
                min_lon,
                min_lat,
                max_lon,
                max_lat,
                status,
                bolme_fetched,
                mescere_fetched,
                bolme_inserted,
                mescere_inserted,
                failed_bolme,
                failed_mescere,
                error
            )
            VALUES (
                :run_pk,
                :tile_key,
                :min_lon,
                :min_lat,
                :max_lon,
                :max_lat,
                :status,
                :bolme_fetched,
                :mescere_fetched,
                :bolme_inserted,
                :mescere_inserted,
                :failed_bolme,
                :failed_mescere,
                :error
            )
            ON CONFLICT (run_id, tile_key) DO UPDATE
            SET status = EXCLUDED.status,
                bolme_fetched = EXCLUDED.bolme_fetched,
                mescere_fetched = EXCLUDED.mescere_fetched,
                bolme_inserted = EXCLUDED.bolme_inserted,
                mescere_inserted = EXCLUDED.mescere_inserted,
                failed_bolme = EXCLUDED.failed_bolme,
                failed_mescere = EXCLUDED.failed_mescere,
                error = EXCLUDED.error,
                processed_at = now()
            """
        ),
        {
            "run_pk": run_pk,
            "tile_key": str(tile_index),
            "min_lon": tile_bbox[0],
            "min_lat": tile_bbox[1],
            "max_lon": tile_bbox[2],
            "max_lat": tile_bbox[3],
            "status": "failed" if failed else "completed",
            "bolme_fetched": fetched[SOURCE_LAYER_BOLME],
            "mescere_fetched": fetched[SOURCE_LAYER_MESCERE],
            "bolme_inserted": inserted[SOURCE_LAYER_BOLME],
            "mescere_inserted": inserted[SOURCE_LAYER_MESCERE],
            "failed_bolme": failures[SOURCE_LAYER_BOLME],
            "failed_mescere": failures[SOURCE_LAYER_MESCERE],
            "error": error,
        },
    )


def _tile_error_text(errors: dict[str, str]) -> str | None:
    parts = [f"{source}: {error}" for source, error in errors.items() if error]
    if not parts:
        return None
    return "; ".join(parts)[:500]


def _update_ingest_run(
    db,
    run_pk: int,
    processed_tiles: int,
    fetched: dict[str, int],
    inserted: dict[str, int],
    failures: dict[str, int],
    *,
    status: str,
    error: str | None = None,
) -> None:
    db.execute(
        text(
            """
            UPDATE stage3_layer_a_ingest_runs
            SET status = :status,
                processed_tiles = :processed_tiles,
                bolme_fetched = :bolme_fetched,
                mescere_fetched = :mescere_fetched,
                bolme_inserted = :bolme_inserted,
                mescere_inserted = :mescere_inserted,
                failed_bolme = :failed_bolme,
                failed_mescere = :failed_mescere,
                error = :error,
                finished_at = CASE
                    WHEN :status = 'running' THEN NULL
                    WHEN :status IN ('completed', 'failed') THEN now()
                    ELSE finished_at
                END
            WHERE id = :run_pk
            """
        ),
        {
            "run_pk": run_pk,
            "status": status,
            "processed_tiles": processed_tiles,
            "bolme_fetched": fetched[SOURCE_LAYER_BOLME],
            "mescere_fetched": fetched[SOURCE_LAYER_MESCERE],
            "bolme_inserted": inserted[SOURCE_LAYER_BOLME],
            "mescere_inserted": inserted[SOURCE_LAYER_MESCERE],
            "failed_bolme": failures[SOURCE_LAYER_BOLME],
            "failed_mescere": failures[SOURCE_LAYER_MESCERE],
            "error": error,
        },
    )


def _mark_ingest_run_failed(
    run_pk: int,
    processed_tiles: int,
    fetched: dict[str, int],
    inserted: dict[str, int],
    failures: dict[str, int],
    error: str,
) -> None:
    with SessionLocal() as db:
        persisted = _ingest_tile_totals(db, run_pk)
        if persisted["processed_tiles"]:
            processed_tiles = persisted["processed_tiles"]
            fetched = {
                SOURCE_LAYER_BOLME: persisted["bolme_fetched"],
                SOURCE_LAYER_MESCERE: persisted["mescere_fetched"],
            }
            inserted = {
                SOURCE_LAYER_BOLME: persisted["bolme_inserted"],
                SOURCE_LAYER_MESCERE: persisted["mescere_inserted"],
            }
            failures = {
                SOURCE_LAYER_BOLME: persisted["failed_bolme"],
                SOURCE_LAYER_MESCERE: persisted["failed_mescere"],
            }
        _update_ingest_run(
            db,
            run_pk,
            processed_tiles,
            fetched,
            inserted,
            failures,
            status="failed",
            error=error,
        )
        db.commit()


def _ingest_tile_totals(db, run_pk: int) -> dict[str, int]:
    row = db.execute(
        text(
            """
            SELECT
                count(*)::integer AS processed_tiles,
                COALESCE(sum(bolme_fetched), 0)::integer AS bolme_fetched,
                COALESCE(sum(mescere_fetched), 0)::integer AS mescere_fetched,
                COALESCE(sum(bolme_inserted), 0)::integer AS bolme_inserted,
                COALESCE(sum(mescere_inserted), 0)::integer AS mescere_inserted,
                COALESCE(sum(failed_bolme), 0)::integer AS failed_bolme,
                COALESCE(sum(failed_mescere), 0)::integer AS failed_mescere
            FROM stage3_layer_a_ingest_tiles
            WHERE run_id = :run_pk
            """
        ),
        {"run_pk": run_pk},
    ).mappings().one()
    return {key: int(value or 0) for key, value in row.items()}


def _record_source_check_from_completed_runs(db, args, region: dict) -> dict:
    run_ids = _parse_run_ids(args.record_source_check_run_ids)
    rows = [
        dict(row)
        for row in db.execute(
            text(
                """
                SELECT
                    id,
                    source,
                    status,
                    tile_size,
                    total_tiles,
                    processed_tiles,
                    bolme_fetched,
                    mescere_fetched,
                    bolme_inserted,
                    mescere_inserted,
                    failed_bolme,
                    failed_mescere,
                    bbox,
                    metadata
                FROM stage3_layer_a_ingest_runs
                WHERE region_id = :region_id
                  AND id = ANY(:run_ids)
                """
            ),
            {"region_id": region["id"], "run_ids": run_ids},
        ).mappings()
    ]
    found_run_ids = {int(row["id"]) for row in rows}
    missing_run_ids = [run_id for run_id in run_ids if run_id not in found_run_ids]
    if missing_run_ids:
        raise RuntimeError(f"Layer A source-check run ids not found for region {region['code']}: {missing_run_ids}")

    aggregate = _aggregate_completed_run_rows(rows)
    args.source = aggregate["source"]
    _record_aggregate_source_checks(
        db,
        args=args,
        region_code=region["code"],
        fetched=aggregate["fetched"],
        failures=aggregate["failures"],
    )
    return aggregate


def _aggregate_completed_run_rows(rows: list[dict]) -> dict:
    if not rows:
        raise ValueError("at least one completed Layer A run is required")

    group_key = None
    shard_count = 0
    shard_indexes: set[int] = set()
    totals = {
        "total_tiles": 0,
        "processed_tiles": 0,
        "fetched": {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0},
        "inserted": {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0},
        "failures": {SOURCE_LAYER_BOLME: 0, SOURCE_LAYER_MESCERE: 0},
    }
    run_ids: list[int] = []
    source = ""

    for row in rows:
        run_id = int(row["id"])
        metadata = _metadata_from_payload(row.get("metadata"))
        row_source = row.get("source") or ""
        if row_source not in {"all", "bolme", "mescere"}:
            raise RuntimeError(f"Layer A run {run_id} has unsupported source: {row_source}")
        if row.get("status") != "completed":
            raise RuntimeError(f"Layer A run {run_id} is not completed")
        total_tiles = int(row.get("total_tiles") or 0)
        processed_tiles = int(row.get("processed_tiles") or 0)
        if total_tiles <= 0 or processed_tiles < total_tiles:
            raise RuntimeError(f"Layer A run {run_id} did not process its full tile set")
        failed_bolme = int(row.get("failed_bolme") or 0)
        failed_mescere = int(row.get("failed_mescere") or 0)
        if failed_bolme or failed_mescere:
            raise RuntimeError(f"Layer A run {run_id} still has failed tiles")
        if int(metadata.get("max_tiles") or 0):
            raise RuntimeError(f"Layer A run {run_id} is a limited rehearsal, not a full source check")
        if (metadata.get("bbox_source") or "region") != "region":
            raise RuntimeError(f"Layer A run {run_id} was not generated from the region bbox")
        row_shard_count = int(metadata.get("tile_shard_count") or 1)
        row_shard_index = int(metadata.get("tile_shard_index") or 0)
        if row_shard_count < 1 or row_shard_index < 0 or row_shard_index >= row_shard_count:
            raise RuntimeError(f"Layer A run {run_id} has invalid shard metadata")

        current_key = (
            row_source,
            row_shard_count,
            str(float(row.get("tile_size") or 0)),
            json.dumps(_bbox_from_payload(row["bbox"]), separators=(",", ":")),
            tuple(metadata.get("tile_intersect_source_layers") or []),
            metadata.get("region_geometry_hash") or "",
        )
        if group_key is None:
            group_key = current_key
            source = row_source
            shard_count = row_shard_count
        elif current_key != group_key:
            raise RuntimeError("Layer A source-check runs must belong to the same shard set")
        if row_shard_index in shard_indexes:
            raise RuntimeError(f"duplicate Layer A shard index in source-check runs: {row_shard_index}")
        shard_indexes.add(row_shard_index)
        run_ids.append(run_id)
        totals["total_tiles"] += total_tiles
        totals["processed_tiles"] += processed_tiles
        totals["fetched"][SOURCE_LAYER_BOLME] += int(row.get("bolme_fetched") or 0)
        totals["fetched"][SOURCE_LAYER_MESCERE] += int(row.get("mescere_fetched") or 0)
        totals["inserted"][SOURCE_LAYER_BOLME] += int(row.get("bolme_inserted") or 0)
        totals["inserted"][SOURCE_LAYER_MESCERE] += int(row.get("mescere_inserted") or 0)
        totals["failures"][SOURCE_LAYER_BOLME] += failed_bolme
        totals["failures"][SOURCE_LAYER_MESCERE] += failed_mescere

    expected_shards = set(range(shard_count))
    if shard_indexes != expected_shards:
        missing = sorted(expected_shards - shard_indexes)
        raise RuntimeError(f"Layer A source-check shard set is incomplete; missing shard indexes: {missing}")
    if source in {"all", "bolme"} and totals["inserted"][SOURCE_LAYER_BOLME] <= 0:
        raise RuntimeError("Layer A source-check has no Bolme features inserted")
    if source in {"all", "mescere"} and totals["inserted"][SOURCE_LAYER_MESCERE] <= 0:
        raise RuntimeError("Layer A source-check has no Mescere features inserted")

    return {
        "source": source,
        "run_ids": sorted(run_ids),
        "tile_shard_count": shard_count,
        "tile_shard_indexes": sorted(shard_indexes),
        **totals,
    }


def _parse_run_ids(value: str) -> list[int]:
    run_ids = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        run_id = int(part)
        if run_id <= 0:
            raise ValueError("run ids must be positive integers")
        run_ids.append(run_id)
    if not run_ids:
        raise ValueError("at least one run id is required")
    return run_ids


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    if not value.strip():
        raise ValueError("bbox is required unless --bbox-from-region is used")
    parts = [float(part.strip()) for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must contain four numbers")
    min_lon, min_lat, max_lon, max_lat = parts
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("bbox min values must be lower than max values")
    return min_lon, min_lat, max_lon, max_lat


def _bbox_from_payload(payload) -> tuple[float, float, float, float]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    return (
        float(payload["min_lon"]),
        float(payload["min_lat"]),
        float(payload["max_lon"]),
        float(payload["max_lat"]),
    )


def _metadata_from_payload(payload) -> dict:
    if not payload:
        return {}
    if isinstance(payload, str):
        return json.loads(payload)
    return dict(payload)


def _load_region_bbox(db, region_code: str) -> tuple[float, float, float, float]:
    row = db.execute(
        text(
            """
            SELECT
                ST_XMin(ST_Envelope(geom)) AS min_lon,
                ST_YMin(ST_Envelope(geom)) AS min_lat,
                ST_XMax(ST_Envelope(geom)) AS max_lon,
                ST_YMax(ST_Envelope(geom)) AS max_lat
            FROM regions
            WHERE code = :region_code
            """
        ),
        {"region_code": region_code},
    ).mappings().one()
    return (
        float(row["min_lon"]),
        float(row["min_lat"]),
        float(row["max_lon"]),
        float(row["max_lat"]),
    )


def _new_features(features: list[OgmFeature], seen: set[str]) -> list[OgmFeature]:
    fresh = []
    for feature in features:
        if feature.source_id in seen:
            continue
        seen.add(feature.source_id)
        fresh.append(feature)
    return fresh


def _iter_bboxes(
    bbox: tuple[float, float, float, float],
    tile_size: float,
) -> Iterable[tuple[float, float, float, float]]:
    if tile_size <= 0:
        raise ValueError("tile_size must be greater than zero")
    min_lon, min_lat, max_lon, max_lat = bbox
    lon = min_lon
    while lon < max_lon:
        next_lon = min(lon + tile_size, max_lon)
        lat = min_lat
        while lat < max_lat:
            next_lat = min(lat + tile_size, max_lat)
            yield lon, lat, next_lon, next_lat
            lat = next_lat
        lon = next_lon


def _tile_work_items(
    db,
    *,
    bbox: tuple[float, float, float, float],
    tile_size: float,
    max_tiles: int,
    region_code: str,
    intersect_source_layers: list[str],
    tile_shard_count: int = 1,
    tile_shard_index: int = 0,
) -> list[dict[str, int | tuple[float, float, float, float]]]:
    if tile_shard_count < 1:
        raise ValueError("tile_shard_count must be at least 1")
    if tile_shard_index < 0 or tile_shard_index >= tile_shard_count:
        raise ValueError("tile_shard_index must be between 0 and tile_shard_count - 1")
    tiles = []
    filtered_index = 0
    for tile_bbox in _iter_bboxes(bbox, tile_size):
        if intersect_source_layers and not _tile_intersects_source_layers(
            db,
            tile_bbox,
            region_code=region_code,
            source_layers=intersect_source_layers,
        ):
            continue
        filtered_index += 1
        if (filtered_index - 1) % tile_shard_count != tile_shard_index:
            continue
        tiles.append({"tile_key": filtered_index, "bbox": tile_bbox})
        if max_tiles and len(tiles) >= max_tiles:
            break
    return tiles


def _tile_intersects_source_layers(
    db,
    bbox: tuple[float, float, float, float],
    *,
    region_code: str,
    source_layers: list[str],
) -> bool:
    return bool(
        db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM ogm_parcels parcel
                    WHERE parcel.is_current
                      AND parcel.source_layer = ANY(:source_layers)
                      AND parcel.attributes->>'region_code' = :region_code
                      AND parcel.geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
                      AND ST_Intersects(
                          parcel.geom,
                          ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
                      )
                )
                """
            ),
            {
                "source_layers": source_layers,
                "region_code": region_code,
                "min_lon": bbox[0],
                "min_lat": bbox[1],
                "max_lon": bbox[2],
                "max_lat": bbox[3],
            },
        ).scalar_one()
    )


def _tile_count(
    bbox: tuple[float, float, float, float],
    tile_size: float,
    max_tiles: int = 0,
) -> int:
    count = 0
    for _ in _iter_bboxes(bbox, tile_size):
        if max_tiles and count >= max_tiles:
            break
        count += 1
    return count


def _parse_source_layers(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _final_run_status(failures: dict[str, int]) -> str:
    return "failed" if sum(failures.values()) else "completed"


def _bbox_payload(bbox: tuple[float, float, float, float]) -> dict[str, float]:
    return {
        "min_lon": bbox[0],
        "min_lat": bbox[1],
        "max_lon": bbox[2],
        "max_lat": bbox[3],
    }


def _short_error(exc: Exception) -> str:
    message = str(exc).splitlines()
    detail = message[0] if message else "no details"
    return f"{type(exc).__name__}: {detail[:240]}"


if __name__ == "__main__":
    main()
