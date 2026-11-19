from __future__ import annotations

from typing import Mapping

from app.core.config import Settings, get_settings


V10_REGION_CODE = "TR-STAGE3-PUBLIC-OGM-OSM-UNION-FOREST-V10"
V10_GRID_VERSION = "stage3-public-ogm-osm-union-clean-v10"
ACTIVE_GRID_CELLS_TABLE = "grid_cells"
PARTITIONED_GRID_CELLS_TABLE = "grid_cells_partitioned"
ACTIVE_SENTINEL_OBSERVATIONS_TABLE = "sentinel_observations"
PARTITIONED_SENTINEL_OBSERVATIONS_TABLE = "sentinel_observations_partitioned"
ACTIVE_VERIFICATION_RESULTS_TABLE = "verification_results"
PARTITIONED_VERIFICATION_RESULTS_TABLE = "verification_results_partitioned"


def grid_cells_table_sql(
    params: Mapping[str, object],
    *,
    settings: Settings | None = None,
) -> str:
    return (
        PARTITIONED_GRID_CELLS_TABLE
        if use_partitioned_v10_grid_reads(params, settings=settings)
        else ACTIVE_GRID_CELLS_TABLE
    )


def use_partitioned_v10_grid_reads(
    params: Mapping[str, object],
    *,
    settings: Settings | None = None,
) -> bool:
    if (str(params.get("region_code") or "").strip(), str(params.get("grid_version") or "").strip()) == (CURRENT_PUBLIC_REGION_CODE, CURRENT_PUBLIC_GRID_VERSION):
        return False
    settings = settings or get_settings()
    return settings.stage3_v10_partitioned_grid_reads and is_v10_public_scope(params)


def sentinel_observations_table_sql(*, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return (
        PARTITIONED_SENTINEL_OBSERVATIONS_TABLE
        if settings.stage3_partitioned_evidence_reads
        else ACTIVE_SENTINEL_OBSERVATIONS_TABLE
    )


def verification_results_table_sql(*, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return (
        PARTITIONED_VERIFICATION_RESULTS_TABLE
        if settings.stage3_partitioned_evidence_reads
        else ACTIVE_VERIFICATION_RESULTS_TABLE
    )


def is_v10_public_scope(params: Mapping[str, object]) -> bool:
    if (str(params.get("region_code") or "").strip(), str(params.get("grid_version") or "").strip()) == (CURRENT_PUBLIC_REGION_CODE, CURRENT_PUBLIC_GRID_VERSION):
        return True
    return (
        str(params.get("region_code") or "").strip() == V10_REGION_CODE
        and str(params.get("grid_version") or "").strip() == V10_GRID_VERSION
    )

CURRENT_PUBLIC_REGION_CODE = "TR-CURRENT-FOREST-20261114"
CURRENT_PUBLIC_GRID_VERSION = "stage3-current-forest-20261114"
