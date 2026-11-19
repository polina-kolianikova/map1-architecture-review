from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.commands.ingest_ogm_test_subset import _insert_current_features, _record_source_check
from app.db.session import SessionLocal
from app.services.ogm import OgmFeature
from app.services.source_strategy import SOURCE_LAYER_OFFICIAL_VECTOR


def main() -> None:
    parser = argparse.ArgumentParser(description="Import an official Layer A vector export")
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--source-id-property", default="")
    parser.add_argument("--official-status", default="official_full_layer_a")
    parser.add_argument("--source-url", default="local_official_export")
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    features = load_geojson_features(Path(args.file), source_id_property=args.source_id_property)

    with SessionLocal() as db:
        region = db.execute(
            text("SELECT id, code FROM regions WHERE code = :region_code"),
            {"region_code": args.region_code},
        ).mappings().one()
        if args.dry_run:
            print(
                "stage3 official layer-a import plan "
                f"region={args.region_code} features={len(features)} file={args.file}"
            )
            return
        inserted = _insert_current_features(
            db,
            region,
            features,
            source_layer=SOURCE_LAYER_OFFICIAL_VECTOR,
            source_url=args.source_url,
            pipeline="stage3_official_layer_a_import",
            official_status=args.official_status,
            timeout=args.timeout,
            tls_verify=True,
            retire_existing=True,
            replace_matching=True,
            clip_to_region=True,
        )
        _record_source_check(
            db,
            source_layer=SOURCE_LAYER_OFFICIAL_VECTOR,
            source_type="official_vector_export",
            region_code=region["code"],
            status="ok" if inserted else "empty",
            feature_count=inserted,
            error=None,
            timeout=args.timeout,
            tls_verify=True,
        )
        db.commit()
    print(f"stage3 official layer-a import finished region={args.region_code} inserted={inserted}")


def load_geojson_features(path: Path, *, source_id_property: str = "") -> list[OgmFeature]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_features = _raw_features(payload)
    features = []
    for index, feature in enumerate(raw_features, start=1):
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry")
        if not geometry:
            continue
        source_id = _source_id(properties, index, source_id_property)
        features.append(
            OgmFeature(
                source_id=source_id,
                attributes=properties,
                geometry=geometry,
            )
        )
    return features


def _raw_features(payload: dict[str, Any]) -> list[dict[str, Any]]:
    payload_type = payload.get("type")
    if payload_type == "FeatureCollection":
        return [feature for feature in payload.get("features", []) if isinstance(feature, dict)]
    if payload_type == "Feature":
        return [payload]
    raise ValueError("GeoJSON file must be a FeatureCollection or Feature")


def _source_id(properties: dict[str, Any], index: int, source_id_property: str) -> str:
    if source_id_property:
        value = properties.get(source_id_property)
        if value is not None and str(value).strip():
            return str(value)
    for key in ("OBJECTID", "objectid", "id", "uuid", "parcel_id"):
        value = properties.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return f"official-{index}"


if __name__ == "__main__":
    main()
