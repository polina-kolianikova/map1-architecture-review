from __future__ import annotations

from typing import Any

from app.core.config import get_settings

SOURCE_LAYER_BOLME = "ogm_esatis_bolme"
SOURCE_LAYER_MESCERE = "ogm_yayin_mescere_wms_svg"
SOURCE_LAYER_OFFICIAL_VECTOR = "official_layer_a_vector"
SOURCE_LAYER_OSM_FOREST = "osm_forest_mask"
SOURCE_LAYER_OSM_NONFOREST_EXCLUSION = "osm_nonforest_exclusion"
SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION = "esa_worldcover_nonforest_exclusion"
LEGACY_SOURCE_LAYER = "ogm_eharita_test_subset"

SOURCE_DEFINITIONS: dict[str, dict[str, Any]] = {
    SOURCE_LAYER_BOLME: {
        "source_type": "arcgis_feature_layer",
        "source_quality": "public_ogm_rest",
        "access_mode": "public",
        "production_role": "active_public_layer_a",
        "geometry_method": "arcgis_rest_query",
    },
    SOURCE_LAYER_MESCERE: {
        "source_type": "wms_svg_feature_info",
        "source_quality": "public_wms_derived",
        "access_mode": "public",
        "production_role": "active_public_layer_a",
        "geometry_method": "wms_svg_path_polygon",
        "geometry_precision": "public_svg_map_geometry",
        "attribute_method": "wms_getfeatureinfo",
        "future_source": "tucbs_ogm_mescere_wfs",
    },
    SOURCE_LAYER_OFFICIAL_VECTOR: {
        "source_type": "official_vector_adapter",
        "source_quality": "official_full_layer_a",
        "access_mode": "official_export_or_authenticated_service",
        "production_role": "future_full_layer_a",
        "geometry_method": "configured_vector_import_or_wfs",
        "status": "backlog",
    },
    SOURCE_LAYER_OSM_FOREST: {
        "source_type": "osm_public",
        "source_quality": "public_osm_forest",
        "access_mode": "public",
        "production_role": "auxiliary_public_forest_mask",
        "geometry_method": "bulk_pbf_or_overpass_natural_wood_landuse_forest",
    },
    SOURCE_LAYER_OSM_NONFOREST_EXCLUSION: {
        "source_type": "osm_public",
        "source_quality": "public_osm_nonforest_exclusion",
        "access_mode": "public",
        "production_role": "auxiliary_public_nonforest_exclusion",
        "geometry_method": "bulk_pbf_or_overpass_roads_buildings_fields_difference",
    },
    SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION: {
        "source_type": "esa_worldcover_public_cog",
        "source_quality": "public_satellite_landcover_nonforest_exclusion",
        "access_mode": "public",
        "production_role": "auxiliary_public_nonforest_exclusion",
        "geometry_method": "worldcover_10m_classes_polygonized_and_clipped",
        "dataset_version": "ESA_WorldCover_10m_2021_v200",
    },
}


SOURCE_NAMES = {
    SOURCE_LAYER_BOLME: "OGM Bolme",
    SOURCE_LAYER_MESCERE: "OGM Mescere",
    SOURCE_LAYER_OFFICIAL_VECTOR: "Official Layer A",
    SOURCE_LAYER_OSM_FOREST: "OpenStreetMap Forest",
    SOURCE_LAYER_OSM_NONFOREST_EXCLUSION: "OpenStreetMap Non-Forest Exclusion",
    SOURCE_LAYER_WORLDCOVER_NONFOREST_EXCLUSION: "ESA WorldCover Non-Forest Exclusion",
}


def source_definition(source_layer: str) -> dict[str, Any]:
    return SOURCE_DEFINITIONS.get(source_layer, {})


def stage1_source_strategy() -> dict[str, Any]:
    active_sources = []
    for source_layer in get_settings().layer_a_source_layers:
        definition = source_definition(source_layer)
        active_sources.append(
            {
                "source_name": SOURCE_NAMES.get(source_layer, source_layer),
                "source_quality": definition.get("source_quality", "configured_layer_a_source"),
                "access_mode": definition.get("access_mode", "configured"),
                "status": "active",
                "role": definition.get("production_role", "configured Layer A polygon source"),
            }
        )

    return {
        "layer": "Layer A",
        "current_mode": "public_data_mvp",
        "active_sources": active_sources,
        "extension_model": {
            "closed_layers_status": "backlog",
            "integration_boundary": "normalized Layer A records",
            "future_adapter_sources": [
                {
                    "source_name": SOURCE_NAMES[SOURCE_LAYER_OFFICIAL_VECTOR],
                    "source_quality": SOURCE_DEFINITIONS[SOURCE_LAYER_OFFICIAL_VECTOR]["source_quality"],
                    "status": SOURCE_DEFINITIONS[SOURCE_LAYER_OFFICIAL_VECTOR]["status"],
                    "expected_input": "official vector export, authenticated WFS, ArcGIS REST, FileGDB, GeoPackage, GeoJSON, or PostGIS dump",
                }
            ],
            "schema_change_policy": (
                "adapter/config integration into the same normalized Layer A contract; "
                "official schemas may require mapping or data migration, not an architecture rewrite"
            ),
        },
    }
