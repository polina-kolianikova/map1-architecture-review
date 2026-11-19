import json
from datetime import UTC, datetime

import requests
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.grid import TEST_REGION_CODE, TEST_REGION_COORDINATES
from app.services.ogm import OgmBolmeClient, OgmFeature, OgmMescereWmsClient
from app.services.source_strategy import (
    LEGACY_SOURCE_LAYER,
    SOURCE_LAYER_BOLME,
    SOURCE_LAYER_MESCERE,
    source_definition,
)

DEFAULT_LIMIT = 1000


def main() -> None:
    with SessionLocal() as db:
        result = ingest_test_subset(db)
        db.commit()

    fresh_count = result["bolme_count"] + result["mescere_count"]
    print(
        "ogm subset stored "
        f"bolme={result['bolme_count']} "
        f"mescere={result['mescere_count']} "
        f"active={result['active_count']}"
    )
    for error in result["errors"]:
        print(
            "ogm source unavailable "
            f"source_layer={error['source_layer']} error={error['error']}"
        )
    if result["bolme_count"] <= 0:
        raise SystemExit(1)
    if result["errors"] and not (fresh_count or result["active_count"]):
        raise SystemExit(1)


def ingest_test_subset(db, timeout: int = 45) -> dict[str, int | list[dict[str, str]]]:
    lock_acquired = db.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"stage1_ogm_ingest:{TEST_REGION_CODE}"},
    ).scalar_one()
    if not lock_acquired:
        raise RuntimeError("Stage 1 Layer A ingest is already running")
    region = db.execute(
        text(
            """
            SELECT id, code
            FROM regions
            WHERE code = :region_code
            """
        ),
        {"region_code": TEST_REGION_CODE},
    ).mappings().one()

    settings = get_settings()
    bbox = _bbox_from_coordinates(TEST_REGION_COORDINATES)
    errors = []
    bolme_count = 0
    mescere_count = 0

    try:
        bolme_features = OgmBolmeClient(
            settings.ogm_bolme_feature_url,
            timeout=timeout,
            verify_tls=settings.ogm_tls_verify,
            proxy_url=settings.ogm_proxy_url,
        ).query_bbox(
            bbox,
            limit=DEFAULT_LIMIT,
        )
    except (requests.RequestException, RuntimeError) as exc:
        errors.append(_source_error(SOURCE_LAYER_BOLME, exc))
        _record_source_check(
            db,
            source_layer=SOURCE_LAYER_BOLME,
            source_type="public_ogm_rest",
            region_code=region["code"],
            status="unavailable",
            feature_count=0,
            error=_short_error(exc),
            timeout=timeout,
            tls_verify=settings.ogm_tls_verify,
        )
    else:
        if bolme_features:
            bolme_count = _insert_current_features(
                db,
                region,
                bolme_features,
                source_layer=SOURCE_LAYER_BOLME,
                source_url=settings.ogm_bolme_feature_url,
                pipeline="stage1_ogm_bolme",
                official_status="official_bolme",
                timeout=timeout,
                tls_verify=settings.ogm_tls_verify,
            )
        _record_source_check(
            db,
            source_layer=SOURCE_LAYER_BOLME,
            source_type="public_ogm_rest",
            region_code=region["code"],
            status="ok" if bolme_count else "empty",
            feature_count=bolme_count,
            error=None,
            timeout=timeout,
            tls_verify=settings.ogm_tls_verify,
        )

    try:
        mescere_features = OgmMescereWmsClient(
            settings.ogm_mescere_wms_url,
            timeout=timeout,
            verify_tls=settings.ogm_tls_verify,
            proxy_url=settings.ogm_proxy_url,
        ).query_bbox(bbox)
    except (requests.RequestException, RuntimeError) as exc:
        errors.append(_source_error(SOURCE_LAYER_MESCERE, exc))
        _record_source_check(
            db,
            source_layer=SOURCE_LAYER_MESCERE,
            source_type="public_wms_derived",
            region_code=region["code"],
            status="unavailable",
            feature_count=0,
            error=_short_error(exc),
            timeout=timeout,
            tls_verify=settings.ogm_tls_verify,
        )
    else:
        if mescere_features:
            mescere_count = _insert_current_features(
                db,
                region,
                mescere_features,
                source_layer=SOURCE_LAYER_MESCERE,
                source_url=settings.ogm_mescere_wms_url,
                pipeline="stage1_ogm_mescere",
                official_status="public_mescere_wms_derived",
                timeout=timeout,
                tls_verify=settings.ogm_tls_verify,
            )
        _record_source_check(
            db,
            source_layer=SOURCE_LAYER_MESCERE,
            source_type="public_wms_derived",
            region_code=region["code"],
            status="ok" if mescere_count else "empty",
            feature_count=mescere_count,
            error=None,
            timeout=timeout,
            tls_verify=settings.ogm_tls_verify,
        )

    if bolme_count or mescere_count:
        _retire_source(db, LEGACY_SOURCE_LAYER, region["code"])

    return {
        "bolme_count": bolme_count,
        "mescere_count": mescere_count,
        "active_count": _active_layer_a_count(db),
        "errors": errors,
    }


def _bbox_from_coordinates(coordinates: list[list[float]]) -> tuple[float, float, float, float]:
    longitudes = [point[0] for point in coordinates]
    latitudes = [point[1] for point in coordinates]
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


def _forest_type(attributes: dict) -> str | None:
    mescere = attributes.get("MESCERE")
    if mescere:
        return str(mescere)
    bolme_no = attributes.get("BLM_NO")
    if bolme_no is None:
        return None
    return f"bolme_{bolme_no}"


def _insert_current_features(
    db,
    region,
    features: list[OgmFeature],
    source_layer: str,
    source_url: str,
    pipeline: str,
    official_status: str,
    timeout: int,
    tls_verify: bool,
    retire_existing: bool = True,
    replace_matching: bool = False,
    clip_to_region: bool = False,
) -> int:
    stored = 0
    stored_ids = []
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")
    for feature in features:
        if replace_matching and feature.source_id:
            _lock_current_feature(db, source_layer, feature.source_id, region["code"])
        attributes = {
            "pipeline": pipeline,
            "region_id": region["id"],
            "region_code": region["code"],
            "source_url": source_url,
            "source_definition": source_definition(source_layer),
            "source_checked_at": checked_at,
            "request_timeout_seconds": timeout,
            "tls_verify": tls_verify,
            "source_attributes": feature.attributes,
        }
        inserted_id = db.execute(
            text(
                """
                WITH source_geometry AS (
                    SELECT ST_SetSRID(ST_GeomFromGeoJSON(:geometry), 4326) AS geom
                ),
                target_region AS (
                    SELECT geom
                    FROM regions
                    WHERE id = :region_id
                ),
                valid_geometry AS (
                    SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(geom), 3)) AS geom
                    FROM source_geometry
                ),
                clipped_geometry AS (
                    SELECT
                        CASE
                            WHEN :clip_to_region THEN ST_Multi(
                                ST_CollectionExtract(
                                    ST_MakeValid(ST_Intersection(valid_geometry.geom, target_region.geom)),
                                    3
                                )
                            )
                            ELSE valid_geometry.geom
                        END AS geom
                    FROM valid_geometry
                    CROSS JOIN target_region
                    WHERE NOT :clip_to_region
                       OR ST_Intersects(valid_geometry.geom, target_region.geom)
                )
                INSERT INTO ogm_parcels (
                    source_id,
                    source_layer,
                    forest_type,
                    tree_age,
                    official_status,
                    attributes,
                    geom,
                    last_seen_at,
                    is_current
                )
                SELECT
                    :source_id,
                    :source_layer,
                    :forest_type,
                    NULL,
                    :official_status,
                    CAST(:attributes AS jsonb),
                    geom,
                    now(),
                    false
                FROM clipped_geometry
                WHERE NOT ST_IsEmpty(geom)
                  AND ST_IsValid(geom)
                RETURNING id
                """
            ),
            {
                "source_id": feature.source_id,
                "source_layer": source_layer,
                "forest_type": _forest_type(feature.attributes),
                "official_status": official_status,
                "attributes": json.dumps(attributes, separators=(",", ":")),
                "geometry": json.dumps(feature.geometry, separators=(",", ":")),
                "region_id": region["id"],
                "clip_to_region": clip_to_region,
            },
        ).scalar_one_or_none()
        if inserted_id is not None:
            if replace_matching:
                db.execute(
                    text(
                        """
                        UPDATE ogm_parcels
                        SET is_current = false
                        WHERE source_layer = :source_layer
                          AND source_id = :source_id
                          AND attributes->>'region_code' = :region_code
                          AND is_current
                        """
                    ),
                    {
                        "source_layer": source_layer,
                        "source_id": feature.source_id,
                        "region_code": region["code"],
                    },
                )
            stored += 1
            stored_ids.append(inserted_id)
    if stored_ids:
        if retire_existing:
            _retire_source(db, source_layer, region["code"])
        db.execute(
            text(
                """
                UPDATE ogm_parcels
                SET is_current = true
                WHERE id = ANY(:stored_ids)
                """
            ),
            {"stored_ids": stored_ids},
        )
    return stored


def _lock_current_feature(db, source_layer: str, source_id: str, region_code: str) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {
            "lock_key": f"ogm_parcels_current:{source_layer}:{source_id}:{region_code}",
        },
    )


def _retire_source(db, source_layer: str, region_code: str) -> None:
    db.execute(
        text(
            """
            UPDATE ogm_parcels
            SET is_current = false
            WHERE source_layer = :source_layer
              AND attributes->>'region_code' = :region_code
            """
        ),
        {"source_layer": source_layer, "region_code": region_code},
    )


def _active_layer_a_count(db) -> int:
    return db.execute(
        text(
            """
            SELECT count(*)
            FROM ogm_parcels
            WHERE source_layer = ANY(:layer_a_source_layers)
              AND is_current
              AND attributes->>'region_code' = :region_code
            """
        ),
        {
            "layer_a_source_layers": get_settings().layer_a_source_layers,
            "region_code": TEST_REGION_CODE,
        },
    ).scalar_one()


def _source_error(source_layer: str, exc: Exception) -> dict[str, str]:
    return {
        "source_layer": source_layer,
        "error": _short_error(exc),
    }


def _record_source_check(
    db,
    source_layer: str,
    source_type: str,
    region_code: str,
    status: str,
    feature_count: int,
    error: str | None,
    timeout: int,
    tls_verify: bool,
    pipeline: str = "stage1_layer_a_source_check",
) -> None:
    db.execute(
        text(
            """
            INSERT INTO source_checks (
                source_layer,
                source_type,
                region_code,
                status,
                feature_count,
                error,
                metadata
            )
            VALUES (
                :source_layer,
                :source_type,
                :region_code,
                :status,
                :feature_count,
                :error,
                CAST(:metadata AS jsonb)
            )
            """
        ),
        {
            "source_layer": source_layer,
            "source_type": source_type,
            "region_code": region_code,
            "status": status,
            "feature_count": feature_count,
            "error": error,
            "metadata": json.dumps(
                {
                    "pipeline": pipeline,
                    "request_timeout_seconds": timeout,
                    "tls_verify": tls_verify,
                },
                separators=(",", ":"),
            ),
        },
    )


def _short_error(exc: Exception) -> str:
    message = str(exc).splitlines()
    detail = message[0] if message else "no details"
    return f"{type(exc).__name__}: {detail[:240]}"


if __name__ == "__main__":
    main()
