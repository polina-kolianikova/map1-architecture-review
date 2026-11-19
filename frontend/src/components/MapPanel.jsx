import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Crosshair, MapPinned, X } from "lucide-react";

import {
  API_BASE_URL,
  ENABLE_MAP_STATS,
  ENABLE_VECTOR_TILES,
  MAPBOX_TOKEN,
  TEST_CENTER,
} from "../config/runtime";
import { useI18n } from "../hooks/useI18n";
import {
  featureBounds,
  fitMapToBounds,
  selectedFeatureCollection,
  svgPaths,
} from "../lib/geojson";
import { cellLabel, formatInteger, formatNumber } from "../lib/formatters";
import { statusLabel } from "../lib/status";
import { FULL_PUBLIC_SCOPE_KEY } from "../config/scopes";

const TILE_BASE_URL = absoluteApiBaseUrl(API_BASE_URL).replace(/\/$/, "");
const CONTEXT_MAP_STYLE = "mapbox://styles/mapbox/satellite-streets-v12";
const CLEAN_MAP_STYLE = "mapbox://styles/mapbox/satellite-v9";
const SELECTED_CELL_MIN_ZOOM = 14.1;
const SELECTED_CELL_MAX_ZOOM = 15.25;
const SELECTED_CELL_PADDING = 52;
const SELECTED_CELL_COMPACT_PADDING = 24;
const PROJECT_FIT_MAX_ZOOM = 14.05;
const PROJECT_FIT_PADDING = 54;
const MANUAL_ZOOM_STEP = 1.4;
// Keep cells selectable when the map overzooms the source's level-16 tiles.
const MAX_TILE_RENDER_ZOOM = 24;
const SOURCE_CONTEXT_COLOR = "#d8c07a";
const SOURCE_CONTEXT_FILL_OPACITY = 0.025;
const SOURCE_CONTEXT_LINE_OPACITY = 0.32;
const MASK_OVERVIEW_COLOR = "#6fb88f";
const MASK_OVERVIEW_LINE_COLOR = "#b7e2ca";
const COUNTRY_MASK_SOURCE_MAX_ZOOM = 3;
const COUNTRY_MASK_MAX_ZOOM = 6;
const MASK_OUTLINE_MIN_ZOOM = 9;
const SELECTABLE_CELL_LAYERS = [
  "grid-fill",
  "grid-outline",
  "stage2-grid-tile-fill",
  "stage2-grid-tile-outline",
  "stage2-layer-c-tile-fill",
  "stage2-layer-c-tile-outline",
];
const MASK_PICK_LAYERS = [
  "stage2-mask-country-fill",
  "stage2-mask-country-outline",
  "stage2-mask-overview-fill",
  "stage2-mask-overview-outline",
];
const DEFAULT_CELL_TILE_MIN_ZOOM = 12;
const FULL_PUBLIC_CELL_TILE_MIN_ZOOM = 13;

function tileUrl(layer, query = "") {
  const suffix = query ? `?${query}` : "";
  return `${TILE_BASE_URL}/stage2/tiles/${layer}/{z}/{x}/{y}.mvt${suffix}`;
}

export function MapPanel({
  layerA,
  regions,
  grid,
  hasToken,
  onPickLocation,
  onSelectCell,
  selectedCellUuid,
  summary,
  visibleCellCount,
  loading,
  error,
  cleanMode,
  hasActiveFilters,
  hasMatchingResults,
  layoutRevision,
  onClearSelection,
  selectedResult,
  scope,
  tileFilterQuery = "",
  filterPage = 0,
}) {
  const { t } = useI18n();
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const manualViewportUntilRef = useRef(0);
  const onPickLocationRef = useRef(onPickLocation);
  const onSelectCellRef = useRef(onSelectCell);
  const scopeRef = useRef(scope);
  const selectedCellRef = useRef(selectedCellUuid);
  const selectedViewportRef = useRef({ uuid: null, hasGeometry: false });
  const scopeIdentity = `${scope?.key || ""}|${scope?.regionCode || ""}|${scope?.gridVersion || ""}`;
  const viewportFitIdentity = `${scopeIdentity}|${tileFilterQuery}|${hasActiveFilters ? filterPage : ""}`;
  const requestedViewportRef = useRef({ identity: viewportFitIdentity, scope: scopeIdentity, pending: hasActiveFilters });
  const initialViewportFitRef = useRef(false);
  const mapStyle = cleanMode ? CLEAN_MAP_STYLE : CONTEXT_MAP_STYLE;
  const appliedStyleRef = useRef(`${mapStyle}|${tileFilterQuery}`);
  const [mapReady, setMapReady] = useState(false);
  const [mapError, setMapError] = useState(false);
  const [showSourceContext, setShowSourceContext] = useState(false);
  const [maskCoverage, setMaskCoverage] = useState({ pending: false, error: "", source: null });
  const [maskRetryRevision, setMaskRetryRevision] = useState(0);
  const [manualViewportRevision, setManualViewportRevision] = useState(0);
  const sourceContextVisible = !cleanMode && showSourceContext;
  const cellTileMinZoom = stage2CellTileMinZoom(scope);
  const expectedVisibleCellCount = mapFeatureCount(grid, visibleCellCount);
  const dataRef = useRef({ grid, layerA, regions, visibleCellCount: expectedVisibleCellCount });
  const projectBounds = useMemo(
    () => scope?.previewBounds || mapFocusBounds(regions, grid, layerA),
    [grid, layerA, regions, scope]
  );
  const fitBounds = useMemo(
    () => (hasActiveFilters && grid.features.length ? featureBounds(grid.features) : projectBounds),
    [grid, hasActiveFilters, projectBounds]
  );
  const applyManualZoom = useCallback((direction) => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    manualViewportUntilRef.current = Date.now() + 1200;
    setManualViewportRevision((revision) => revision + 1);
    map.stop();
    const currentZoom = map.getZoom();
    const nextZoom =
      direction === "in"
        ? Math.min(currentZoom + MANUAL_ZOOM_STEP, map.getMaxZoom())
        : Math.max(currentZoom - MANUAL_ZOOM_STEP, map.getMinZoom());
    const publishZoom = () => {
      if (!map.isStyleLoaded()) {
        return;
      }
      writeMapStats({
        ...readMapStats(),
        selectedCell: selectedCellRef.current,
        ...mapRenderStats(map, Boolean(selectedCellRef.current), dataRef.current.visibleCellCount),
      });
    };
    map.jumpTo({ center: map.getCenter(), zoom: nextZoom });
    publishZoom();
    window.requestAnimationFrame(publishZoom);
  }, []);
  const installMapLayers = useCallback((map, current) => {
    safeResize(map);

    map.addSource("regions", { type: "geojson", data: current.regions });
    map.addSource("layer-a", { type: "geojson", data: current.layerA });
    map.addLayer({
      id: "layer-a-fill",
      type: "fill",
      source: "layer-a",
      paint: {
        "fill-color": SOURCE_CONTEXT_COLOR,
        "fill-opacity": SOURCE_CONTEXT_FILL_OPACITY,
      },
    });
    map.addLayer({
      id: "layer-a-outline",
      type: "line",
      source: "layer-a",
      paint: {
        "line-color": SOURCE_CONTEXT_COLOR,
        "line-width": 0.8,
        "line-opacity": SOURCE_CONTEXT_LINE_OPACITY,
        "line-dasharray": [1.2, 1.2],
      },
    });
    map.addSource("grid", { type: "geojson", data: current.grid });
    map.addSource("selected-cell", {
      type: "geojson",
      data: selectedFeatureCollection(current.grid, selectedCellRef.current),
    });
    if (ENABLE_VECTOR_TILES) {
      map.addSource("stage2-grid-tiles", {
        type: "vector",
        tiles: [tileUrl("grid", tileFilterQuery)],
        minzoom: DEFAULT_CELL_TILE_MIN_ZOOM,
        maxzoom: 16,
      });
      map.addSource("stage2-mask-country-tiles", {
        type: "vector",
        tiles: [tileUrl("mask", tileFilterQuery)],
        minzoom: 0,
        maxzoom: COUNTRY_MASK_SOURCE_MAX_ZOOM,
      });
      map.addSource("stage2-mask-tiles", {
        type: "vector",
        tiles: [tileUrl("mask", tileFilterQuery)],
        minzoom: COUNTRY_MASK_MAX_ZOOM,
        maxzoom: 16,
      });
      map.addSource("stage2-layer-a-tiles", {
        type: "vector",
        tiles: [tileUrl("layer-a", tileFilterQuery)],
        minzoom: 0,
        maxzoom: 16,
      });
      map.addSource("stage2-layer-b-tiles", {
        type: "vector",
        tiles: [tileUrl("layer-b", tileFilterQuery)],
        minzoom: DEFAULT_CELL_TILE_MIN_ZOOM,
        maxzoom: 16,
      });
      map.addSource("stage2-layer-c-tiles", {
        type: "vector",
        tiles: [tileUrl("layer-c", tileFilterQuery)],
        minzoom: DEFAULT_CELL_TILE_MIN_ZOOM,
        maxzoom: 16,
      });
      // The country fill remains available beneath a loading finer source.
      // Its visibility, rather than a hard zoom cutoff, controls the handoff.
      map.addLayer({
        id: "stage2-mask-country-fill",
        type: "fill",
        source: "stage2-mask-country-tiles",
        "source-layer": "mask",
        paint: {
          "fill-color": MASK_OVERVIEW_COLOR,
          "fill-opacity": scope?.isPreview ? 0.26 : 0.16,
          "fill-antialias": false,
        },
      });
      map.addLayer({
        id: "stage2-mask-country-outline",
        type: "line",
        source: "stage2-mask-country-tiles",
        "source-layer": "mask",
        minzoom: MASK_OUTLINE_MIN_ZOOM,
        paint: {
          "line-color": MASK_OVERVIEW_LINE_COLOR,
          "line-width": scope?.isPreview ? 1.1 : 0.7,
          "line-opacity": scope?.isPreview ? 0.72 : 0.45,
        },
      });
      map.addLayer({
        id: "stage2-mask-overview-fill",
        type: "fill",
        source: "stage2-mask-tiles",
        "source-layer": "mask",
        minzoom: COUNTRY_MASK_MAX_ZOOM,
        maxzoom: cellTileMinZoom,
        paint: {
          "fill-color": MASK_OVERVIEW_COLOR,
          "fill-opacity": scope?.isPreview ? 0.26 : 0.16,
          "fill-antialias": false,
        },
      });
      map.addLayer({
        id: "stage2-mask-overview-outline",
        type: "line",
        source: "stage2-mask-tiles",
        "source-layer": "mask",
        minzoom: MASK_OUTLINE_MIN_ZOOM,
        maxzoom: cellTileMinZoom,
        paint: {
          "line-color": MASK_OVERVIEW_LINE_COLOR,
          "line-width": scope?.isPreview ? 1.1 : 0.7,
          "line-opacity": scope?.isPreview ? 0.72 : 0.45,
        },
      });
      map.addLayer({
        id: "stage2-grid-tile-fill",
        type: "fill",
        source: "stage2-grid-tiles",
        "source-layer": "grid",
        minzoom: cellTileMinZoom,
        paint: {
          "fill-color": gridStatusFillColorExpression(cleanMode, "status"),
          "fill-opacity": scope?.isPreview ? 0.22 : stage2GridFillOpacity(cleanMode),
        },
      });
      map.addLayer({
        id: "stage2-layer-a-tile-fill",
        type: "fill",
        source: "stage2-layer-a-tiles",
        "source-layer": "layer_a",
        paint: {
          "fill-color": SOURCE_CONTEXT_COLOR,
          "fill-opacity": SOURCE_CONTEXT_FILL_OPACITY,
        },
      });
      map.addLayer({
        id: "stage2-layer-b-tile-fill",
        type: "fill",
        source: "stage2-layer-b-tiles",
        "source-layer": "layer_b",
        minzoom: cellTileMinZoom,
        paint: {
          "fill-color": ndviColorExpression("ndvi"),
          "fill-opacity": layerBFillOpacity(cleanMode, 0.22),
        },
      });
      map.addLayer({
        id: "stage2-layer-c-tile-fill",
        type: "fill",
        source: "stage2-layer-c-tiles",
        "source-layer": "layer_c",
        minzoom: cellTileMinZoom,
        paint: {
          "fill-color": verificationColorExpression("status"),
          "fill-opacity": [
            "match",
            ["get", "status"],
            "ok",
            0.16,
            "watch",
            0.25,
            "anomaly",
            0.32,
            "insufficient_data",
            0.14,
            0.08,
          ],
        },
      });
      map.addLayer({
        id: "stage2-layer-a-tile-outline",
        type: "line",
        source: "stage2-layer-a-tiles",
        "source-layer": "layer_a",
        paint: {
          "line-color": SOURCE_CONTEXT_COLOR,
          "line-width": 0.8,
          "line-opacity": SOURCE_CONTEXT_LINE_OPACITY,
          "line-dasharray": [1.2, 1.2],
        },
      });
      map.addLayer({
        id: "stage2-grid-tile-outline",
        type: "line",
        source: "stage2-grid-tiles",
        "source-layer": "grid",
        minzoom: cellTileMinZoom,
        paint: {
          "line-color": gridStatusLineColorExpression(cleanMode, "status"),
          "line-width": scope?.isPreview ? 1.4 : 0.8,
          "line-opacity": scope?.isPreview ? 0.9 : 0.45,
        },
      });
      map.addLayer({
        id: "stage2-layer-c-tile-outline",
        type: "line",
        source: "stage2-layer-c-tiles",
        "source-layer": "layer_c",
        minzoom: cellTileMinZoom,
        paint: {
          "line-color": verificationColorExpression("status"),
          "line-width": [
            "match",
            ["get", "status"],
            "watch",
            1.6,
            "anomaly",
            2,
            1.1,
          ],
          "line-opacity": cleanMode ? 0.86 : 0.78,
        },
      });
    }
    map.addLayer({
      id: "layer-b-fill",
      type: "fill",
      source: "grid",
      paint: {
        "fill-color": ndviColorExpression("ndvi"),
        "fill-opacity": layerBFillOpacity(cleanMode, 0.2),
      },
    });
    map.addLayer({
      id: "grid-fill",
      type: "fill",
      source: "grid",
      paint: {
        "fill-color": gridStatusFillColorExpression(cleanMode, "verificationStatus"),
        "fill-opacity": gridFillOpacityExpression(cleanMode, "verificationStatus"),
      },
    });
    map.addLayer({
      id: "grid-outline",
      type: "line",
      source: "grid",
      paint: {
        "line-color": gridStatusLineColorExpression(cleanMode, "verificationStatus"),
        "line-width": [
          "match",
          ["get", "verificationStatus"],
          "watch",
          1.6,
          "anomaly",
          2,
          1.2,
        ],
        "line-opacity": cleanMode ? 0.86 : 0.78,
      },
    });
    syncMapLayerVisibility(map, cleanMode, sourceContextVisible, scope, hasActiveFilters);
    syncMapLayerZoomRanges(map, scope);
    syncBaseMapContextVisibility(map, cleanMode);
    syncModePaint(map, cleanMode, scope?.isPreview);
    map.addLayer({
      id: "selected-cell-fill",
      type: "fill",
      source: "selected-cell",
      paint: {
        "fill-color": "#f8fafc",
        "fill-opacity": 0.2,
      },
    });
    map.addLayer({
      id: "selected-cell-outline",
      type: "line",
      source: "selected-cell",
      paint: {
        "line-color": "#ffffff",
        "line-width": 4.4,
        "line-opacity": 0.96,
      },
    });
    map.addLayer({
      id: "region-outline",
      type: "line",
      source: "regions",
      paint: {
        "line-color": cleanMode ? "#e8f5ed" : "#1f2937",
        "line-width": cleanMode ? 1.4 : 2.2,
        "line-opacity": cleanMode ? 0.42 : 0.8,
      },
    });
    if (!initialViewportFitRef.current) {
      fitMapToBounds(map, current.bounds, projectFitOptions(scope?.isPreview));
      initialViewportFitRef.current = true;
    }
    writeMapStats({
      live: true,
      vectorTiles: ENABLE_VECTOR_TILES,
      gridFeatures: current.visibleCellCount,
      expectedGridFeatures: current.visibleCellCount,
      sourceGridFeatures: ENABLE_VECTOR_TILES ? 0 : current.grid.features.length,
      layerAFeatures: current.layerA.features.length,
      layerBFeatures: mapFeatureCount(current.grid, current.visibleCellCount),
      layerCFeatures: current.visibleCellCount,
      regionFeatures: current.regions.features.length,
      selectedCell: selectedCellRef.current,
      renderedGridFeatures: 0,
      renderedSelectedFeatures: 0,
      zoom: map.getZoom(),
    });
    map.once("idle", () => {
      writeMapStats({
        ...readMapStats(),
        ...mapRenderStats(map, Boolean(selectedCellRef.current), current.grid.features.length),
      });
    });
    setMapError(false);
    setMapReady(true);
  }, [cellTileMinZoom, cleanMode, hasActiveFilters, scope, sourceContextVisible, tileFilterQuery]);

  useEffect(() => {
    onPickLocationRef.current = onPickLocation;
  }, [onPickLocation]);

  useEffect(() => {
    onSelectCellRef.current = onSelectCell;
  }, [onSelectCell]);

  useEffect(() => {
    scopeRef.current = scope;
  }, [scope]);

  useEffect(() => {
    selectedCellRef.current = selectedCellUuid;
  }, [grid, mapReady, selectedCellUuid]);

  useEffect(() => {
    dataRef.current = { grid, layerA, regions, bounds: fitBounds, visibleCellCount: expectedVisibleCellCount };
    const map = mapRef.current;
    writeMapStats({
      live: Boolean(map && hasToken),
      vectorTiles: ENABLE_VECTOR_TILES,
      gridFeatures: expectedVisibleCellCount,
      expectedGridFeatures: expectedVisibleCellCount,
      sourceGridFeatures: ENABLE_VECTOR_TILES ? 0 : grid.features.length,
      layerAFeatures: layerA.features.length,
      layerBFeatures: expectedVisibleCellCount,
      layerCFeatures: expectedVisibleCellCount,
      regionFeatures: regions.features.length,
      selectedCell: selectedCellUuid,
      ...(map?.isStyleLoaded() ? mapRenderStats(map, Boolean(selectedCellUuid), expectedVisibleCellCount) : {}),
    });
  }, [expectedVisibleCellCount, fitBounds, grid, hasToken, layerA, regions, selectedCellUuid]);

  useEffect(() => {
    if (!hasToken || !containerRef.current || mapRef.current) {
      return;
    }

    let cancelled = false;
    let styleLoadTimer;

    import("mapbox-gl").then(({ default: mapboxgl }) => {
      if (cancelled || !containerRef.current) {
        return;
      }

      mapboxgl.accessToken = MAPBOX_TOKEN;
      const map = new mapboxgl.Map({
        container: containerRef.current,
        style: mapStyle,
        center: TEST_CENTER,
        zoom: 9.2,
        attributionControl: true,
      });
      if (ENABLE_MAP_STATS) {
        window.__map1Map = map;
      }
      styleLoadTimer = window.setTimeout(() => {
        if (!cancelled && !map.isStyleLoaded()) {
          setMapError(true);
        }
      }, 30000);

      map.once("style.load", () => {
        window.clearTimeout(styleLoadTimer);
        const current = dataRef.current;
        const handleMapClick = (event) => {
          const cellFeatures = renderedFeaturesAt(map, event.point, SELECTABLE_CELL_LAYERS);
          const uuid = cellFeatures.find((feature) => feature.properties?.uuid)?.properties?.uuid;
          if (uuid) {
            onSelectCellRef.current?.(uuid);
            return;
          }
          if (!scopeRef.current?.isPreview
            || map.getZoom() >= stage2CellTileMinZoom(scopeRef.current)) {
            return;
          }
          const maskFeatures = renderedFeaturesAt(map, event.point, MASK_PICK_LAYERS);
          if (maskFeatures.length) {
            onPickLocationRef.current?.({
              lat: event.lngLat.lat,
              lng: event.lngLat.lng,
            });
          }
        };
        map.on("click", handleMapClick);
        const showPointer = () => {
          map.getCanvas().style.cursor = "pointer";
        };
        const showPreviewPointer = () => {
          if (scopeRef.current?.isPreview) {
            showPointer();
          }
        };
        const hidePointer = () => {
          map.getCanvas().style.cursor = "";
        };
        map.on("mouseenter", "grid-fill", showPointer);
        map.on("mouseleave", "grid-fill", hidePointer);
        map.on("mouseenter", "grid-outline", showPointer);
        map.on("mouseleave", "grid-outline", hidePointer);
        if (ENABLE_VECTOR_TILES) {
          map.on("mouseenter", "stage2-grid-tile-fill", showPointer);
          map.on("mouseleave", "stage2-grid-tile-fill", hidePointer);
          map.on("mouseenter", "stage2-grid-tile-outline", showPointer);
          map.on("mouseleave", "stage2-grid-tile-outline", hidePointer);
          map.on("mouseenter", "stage2-layer-c-tile-fill", showPointer);
          map.on("mouseleave", "stage2-layer-c-tile-fill", hidePointer);
          map.on("mouseenter", "stage2-layer-c-tile-outline", showPointer);
          map.on("mouseleave", "stage2-layer-c-tile-outline", hidePointer);
          map.on("mouseenter", "stage2-mask-overview-fill", showPreviewPointer);
          map.on("mouseleave", "stage2-mask-overview-fill", hidePointer);
          map.on("mouseenter", "stage2-mask-overview-outline", showPreviewPointer);
          map.on("mouseleave", "stage2-mask-overview-outline", hidePointer);
          map.on("mouseenter", "stage2-mask-country-fill", showPreviewPointer);
          map.on("mouseleave", "stage2-mask-country-fill", hidePointer);
          map.on("mouseenter", "stage2-mask-country-outline", showPreviewPointer);
          map.on("mouseleave", "stage2-mask-country-outline", hidePointer);
        }
        const publishViewportStats = () => {
          if (!map.isStyleLoaded()) {
            return;
          }
          writeMapStats({
            ...readMapStats(),
            selectedCell: selectedCellRef.current,
            ...mapRenderStats(map, Boolean(selectedCellRef.current), dataRef.current.visibleCellCount),
          });
        };
        map.on("moveend", publishViewportStats);
        map.on("zoomend", publishViewportStats);
        installMapLayers(map, current);
      });

      mapRef.current = map;
    }).catch(() => setMapError(true));

    return () => {
      cancelled = true;
      selectedViewportRef.current = { uuid: null, hasGeometry: false };
      initialViewportFitRef.current = false;
      setMapReady(false);
      if (styleLoadTimer) {
        window.clearTimeout(styleLoadTimer);
      }
      mapRef.current?.remove();
      mapRef.current = null;
      if (ENABLE_MAP_STATS) {
        delete window.__map1Map;
      }
    };
  }, [hasToken]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) {
      return;
    }
    const styleKey = `${mapStyle}|${tileFilterQuery}`;
    if (appliedStyleRef.current === styleKey) {
      return;
    }
    const [appliedMapStyle] = appliedStyleRef.current.split("|");
    if (appliedMapStyle === mapStyle) {
      appliedStyleRef.current = styleKey;
      refreshVectorTileSources(map, tileFilterQuery);
      return;
    }
    appliedStyleRef.current = styleKey;
    setMapReady(false);
    map.once("style.load", () => {
      installMapLayers(map, dataRef.current);
    });
    map.setStyle(mapStyle);
  }, [installMapLayers, mapReady, mapStyle, tileFilterQuery]);

  useEffect(() => {
    if (!hasToken || !containerRef.current || typeof ResizeObserver === "undefined") {
      return undefined;
    }

    const observer = new ResizeObserver(() => {
      window.requestAnimationFrame(() => {
        const map = mapRef.current;
        const box = containerRef.current?.getBoundingClientRect();
        if (!map || !box || box.width < 1 || box.height < 1) {
          return;
        }
        safeResize(map);
      });
    });
    observer.observe(containerRef.current);

    return () => observer.disconnect();
  }, [hasToken]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return undefined;
    }
    const resizeAndFit = () => {
      const box = containerRef.current?.getBoundingClientRect();
      if (!mapRef.current || !box || box.width < 1 || box.height < 1) {
        return;
      }
      safeResize(map);
      if (!map.getSource("selected-cell")) {
        return;
      }
      const selected = selectedFeatureCollection(dataRef.current.grid, selectedCellRef.current);
      if (selected.features.length) {
        fitMapToBounds(map, featureBounds(selected.features), {
          duration: 180,
          ...selectedCellFitOptions(map),
        });
      }
      writeMapStats({
        ...readMapStats(),
        selectedCell: selectedCellRef.current,
        ...mapRenderStats(map, Boolean(selected.features.length), dataRef.current.visibleCellCount),
      });
    };
    const animationFrame = window.requestAnimationFrame(resizeAndFit);
    const timeout = window.setTimeout(resizeAndFit, 180);
    return () => {
      window.cancelAnimationFrame(animationFrame);
      window.clearTimeout(timeout);
    };
  }, [layoutRevision, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.getSource("selected-cell")) {
      return;
    }
    map.getSource("regions")?.setData(regions);
    map.getSource("grid")?.setData(grid);
    map.getSource("layer-a")?.setData(layerA);
    map.getSource("selected-cell")?.setData(selectedFeatureCollection(grid, selectedCellRef.current));
    const selected = selectedFeatureCollection(grid, selectedCellRef.current);
    writeMapStats({
      live: true,
      vectorTiles: ENABLE_VECTOR_TILES,
      gridFeatures: expectedVisibleCellCount,
      expectedGridFeatures: expectedVisibleCellCount,
      sourceGridFeatures: ENABLE_VECTOR_TILES ? 0 : grid.features.length,
      layerAFeatures: layerA.features.length,
      layerBFeatures: expectedVisibleCellCount,
      layerCFeatures: expectedVisibleCellCount,
      regionFeatures: regions.features.length,
      selectedCell: selectedCellRef.current,
      ...mapRenderStats(map, Boolean(selectedCellRef.current), expectedVisibleCellCount),
    });
    map.once("idle", () => {
      writeMapStats({
        ...readMapStats(),
        ...mapRenderStats(map, Boolean(selectedCellRef.current), expectedVisibleCellCount),
      });
    });
  }, [expectedVisibleCellCount, fitBounds, grid, layerA, mapReady, regions]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.getSource("selected-cell")) {
      return;
    }
    syncMapLayerVisibility(map, cleanMode, sourceContextVisible, scope, hasActiveFilters);
    syncMapLayerZoomRanges(map, scope);
    syncBaseMapContextVisibility(map, cleanMode);
    syncModePaint(map, cleanMode, scope?.isPreview);
  }, [cleanMode, grid, hasActiveFilters, mapReady, scope, sourceContextVisible]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ENABLE_VECTOR_TILES || !mapReady || !map?.getLayer("stage2-mask-country-fill")) {
      return undefined;
    }
    if (hasActiveFilters) {
      setMaskCoverage({ pending: false, error: "", source: null });
      return undefined;
    }
    const failedSources = new Map();
    let viewportPending = true;
    let lastStatus;
    const sync = () => {
      const status = syncCountryMaskFallback(map, cellTileMinZoom, viewportPending, failedSources);
      if (!lastStatus || status.pending !== lastStatus.pending || status.error !== lastStatus.error
        || status.source !== lastStatus.source) {
        lastStatus = status;
        setMaskCoverage(status);
      }
    };
    const moved = () => {
      viewportPending = true;
      sync();
    };
    // A render follows Mapbox's viewport/source coverage update. Avoid accepting
    // an old source-loaded flag immediately after the camera changes.
    const rendered = () => {
      viewportPending = false;
      sync();
    };
    const sourceData = (event) => {
      if (["stage2-mask-country-tiles", "stage2-mask-tiles", "stage2-grid-tiles"].includes(event.sourceId)) sync();
    };
    const sourceError = (event) => {
      if (["stage2-mask-country-tiles", "stage2-mask-tiles", "stage2-grid-tiles"].includes(event.sourceId)) {
        const message = String(event.error?.message || "Map source error")
          .replace(/access_token=[^&\s]+/g, "access_token=REDACTED").slice(0, 220);
        failedSources.set(event.sourceId, message);
        sync();
      }
    };
    map.on("move", moved);
    map.on("render", rendered);
    map.on("sourcedata", sourceData);
    map.on("error", sourceError);
    sync();
    return () => {
      map.off("move", moved);
      map.off("render", rendered);
      map.off("sourcedata", sourceData);
      map.off("error", sourceError);
    };
  }, [cellTileMinZoom, hasActiveFilters, mapReady, maskRetryRevision, tileFilterQuery]);

  useEffect(() => {
    const previous = requestedViewportRef.current;
    if (previous.identity !== viewportFitIdentity) {
      requestedViewportRef.current = { identity: viewportFitIdentity, scope: scopeIdentity,
        scopeChanged: previous.scope !== scopeIdentity || (previous.pending && previous.scopeChanged),
        pending: true, gridAtChange: grid };
    }
    const request = requestedViewportRef.current;
    const map = mapRef.current;
    if (!request.pending || !mapReady || !map || loading || error || selectedCellUuid) return undefined;
    let bounds;
    if (request.scopeChanged || !hasActiveFilters) {
      bounds = scope?.previewBounds || (regions.features.length ? featureBounds(regions.features) : null);
    } else if (grid !== request.gridAtChange && grid.features.length
      && grid.features.every((feature) => !feature.properties?.regionCode
        || feature.properties.regionCode === scope?.regionCode)) {
      bounds = featureBounds(grid.features);
    }
    if (!bounds) return undefined;
    const fit = () => {
      if (requestedViewportRef.current !== request || mapRef.current !== map || selectedCellRef.current
        || Date.now() < manualViewportUntilRef.current) return;
      fitMapToBounds(map, bounds, projectFitOptions(scope?.isPreview));
      request.pending = false;
      selectedViewportRef.current = { uuid: null, hasGeometry: false };
    };
    const delay = manualViewportUntilRef.current - Date.now();
    if (delay > 0) {
      const timeout = window.setTimeout(fit, delay + 16);
      return () => window.clearTimeout(timeout);
    }
    fit();
    return undefined;
  }, [viewportFitIdentity, scopeIdentity, scope, tileFilterQuery, grid, regions, mapReady,
    loading, error, selectedCellUuid, hasActiveFilters, manualViewportRevision]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.getSource("selected-cell")) {
      return;
    }

    const selected = selectedFeatureCollection(dataRef.current.grid, selectedCellUuid);
    map.getSource("selected-cell")?.setData(selected);
    const hasSelectedFeature = selected.features.length > 0;
    if (map.getLayer("selected-cell-fill")) {
      map.setPaintProperty("selected-cell-fill", "fill-opacity", hasSelectedFeature ? 0.2 : 0);
    }
    if (map.getLayer("selected-cell-outline")) {
      map.setPaintProperty("selected-cell-outline", "line-opacity", hasSelectedFeature ? 0.96 : 0);
    }
    const previous = selectedViewportRef.current;
    const uuid = selectedCellUuid || null;
    const selectionChanged = previous.uuid !== uuid;
    const geometryArrived = Boolean(uuid && hasSelectedFeature && !previous.hasGeometry);
    if ((!selectionChanged && !geometryArrived) || (uuid && !hasSelectedFeature)
      || (!uuid && requestedViewportRef.current.pending)) {
      writeMapStats({
        ...readMapStats(),
        selectedCell: selectedCellUuid,
        ...mapRenderStats(map, hasSelectedFeature, dataRef.current.visibleCellCount),
      });
      return;
    }
    const publishSelectedStats = () => {
      writeMapStats({
        ...readMapStats(),
        selectedCell: selectedCellUuid,
        ...mapRenderStats(map, hasSelectedFeature, dataRef.current.visibleCellCount),
      });
    };
    let statsTimeout;
    let retryTimeout;
    let cancelled = false;
    const fit = () => {
      if (cancelled || mapRef.current !== map || (selectedCellRef.current || null) !== uuid
        || Date.now() < manualViewportUntilRef.current) return;
      if (hasSelectedFeature) {
        fitMapToBounds(map, featureBounds(selected.features), {
          duration: 420,
          ...selectedCellFitOptions(map),
        });
      } else {
        fitMapToBounds(map, dataRef.current.bounds, {
          ...projectFitOptions(scope?.isPreview),
          duration: 300,
        });
      }
      selectedViewportRef.current = { uuid, hasGeometry: hasSelectedFeature };
      publishSelectedStats();
      map.once("moveend", publishSelectedStats);
      statsTimeout = window.setTimeout(publishSelectedStats, hasSelectedFeature ? 680 : 360);
    };
    const delay = manualViewportUntilRef.current - Date.now();
    if (delay > 0) retryTimeout = window.setTimeout(fit, delay + 16);
    else fit();
    return () => {
      cancelled = true;
      window.clearTimeout(retryTimeout);
      window.clearTimeout(statsTimeout);
      map.off("moveend", publishSelectedStats);
    };
  }, [grid, mapReady, selectedCellUuid, manualViewportRevision, viewportFitIdentity]);

  if (!hasToken) {
    writeMapStats({
      live: false,
      vectorTiles: false,
      gridFeatures: grid.features.length,
      layerAFeatures: layerA.features.length,
      layerBFeatures: countLayerBFeatures(grid),
      layerCFeatures: grid.features.length,
      regionFeatures: regions.features.length,
      selectedCell: selectedCellUuid,
    });
    return (
      <StaticMap
        layerA={layerA}
        regions={regions}
        grid={grid}
        onSelectCell={onSelectCell}
        onClearSelection={onClearSelection}
        selectedResult={selectedResult}
        selectedCellUuid={selectedCellUuid}
        scope={scope}
        summary={summary}
        filtered={hasActiveFilters}
        shownCells={visibleCellCount}
        cleanMode={cleanMode}
        note={mapNotice({ error, hasActiveFilters, hasMatchingResults, loading, noToken: true, t })}
      />
    );
  }

  return (
    <>
      {(!mapReady || mapError) && (
        <div className="map-fallback">
          <StaticMap
            layerA={layerA}
            regions={regions}
            grid={grid}
            onSelectCell={onSelectCell}
            onClearSelection={onClearSelection}
            selectedResult={selectedResult}
            selectedCellUuid={selectedCellUuid}
            scope={scope}
            summary={summary}
            filtered={hasActiveFilters}
            shownCells={visibleCellCount}
            cleanMode={cleanMode}
            note={mapNotice({ error, hasActiveFilters, hasMatchingResults, loading, mapError, mapReady, t })}
          />
        </div>
      )}
      <div className={`map-canvas ${mapReady && !mapError ? "is-ready" : "is-loading"}`} ref={containerRef} />
      {mapReady && !mapError && (
        <MapScopeBadge
          filtered={hasActiveFilters}
          scope={scope}
          shownCells={visibleCellCount}
          summary={summary}
        />
      )}
      {mapReady && !mapError && hasActiveFilters && !hasMatchingResults && !loading && !error && (
        <div className="map-empty-state" role="status">
          {t("noCellsMatchFilters")}
        </div>
      )}
      {mapReady && !mapError && !hasActiveFilters && (maskCoverage.pending || maskCoverage.error) && (
        <div className="map-empty-state" role={maskCoverage.error ? "alert" : "status"}>
          <span>
            {maskCoverage.error
              ? `${t("mapDetailUnavailable", { defaultValue: "Map detail unavailable" })}: ${maskCoverage.error}`
              : t("mapDetailLoading", { defaultValue: "Loading map detail; overview shown while it loads" })}
          </span>
          {maskCoverage.error && (
            <button
              className="icon-button"
              style={{ pointerEvents: "auto", marginTop: 8 }}
              type="button"
              onClick={() => {
                const source = mapRef.current?.getSource(maskCoverage.source);
                if (source?.setTiles) {
                  source.setTiles([tileUrl(maskCoverage.source === "stage2-grid-tiles" ? "grid" : "mask", tileFilterQuery)]);
                  setMaskRetryRevision((revision) => revision + 1);
                }
              }}
            >
              {t("retryMapDetail", { defaultValue: "Retry map detail" })}
            </button>
          )}
        </div>
      )}
      {mapReady && !mapError && selectedResult && (
        <MapCellPopup onClearSelection={onClearSelection} row={selectedResult} />
      )}
      {mapReady && !mapError && (
        <button
          className="map-reset-button"
          onClick={() => {
            fitMapToBounds(mapRef.current, fitBounds, projectFitOptions(scope?.isPreview));
          }}
          type="button"
        >
          <Crosshair size={15} />
          <span>{t("fitArea")}</span>
        </button>
      )}
      {mapReady && !mapError && !cleanMode && (
        <button
          aria-pressed={sourceContextVisible}
          className={`map-source-toggle ${sourceContextVisible ? "is-active" : ""}`}
          onClick={() => setShowSourceContext((current) => !current)}
          type="button"
        >
          {sourceContextVisible ? t("hideSourceContext") : t("showSourceContext")}
        </button>
      )}
      {mapReady && !mapError && (
        <div className="map-zoom-controls" aria-label={t("mapZoomControls")}>
          <button
            aria-label={t("zoomIn")}
            onClick={() => applyManualZoom("in")}
            type="button"
          >
            +
          </button>
          <button
            aria-label={t("zoomOut")}
            onClick={() => applyManualZoom("out")}
            type="button"
          >
            -
          </button>
        </div>
      )}
      {mapReady && !mapError && (
        <MapLegend
          cleanMode={cleanMode}
          dimmed={hasActiveFilters && !hasMatchingResults && !loading && !error}
          showSourceContext={sourceContextVisible}
        />
      )}
    </>
  );
}

function StaticMap({
  layerA,
  regions,
  grid,
  onSelectCell,
  onClearSelection,
  selectedCellUuid,
  selectedResult,
  summary,
  scope,
  filtered = false,
  shownCells,
  cleanMode = false,
  note = "Operational fallback view",
}) {
  const { t } = useI18n();
  const displayNote = note === "Operational fallback view" ? t("operationalFallbackView") : note;
  const bounds = useMemo(() => mapFocusBounds(regions, grid, layerA), [grid, layerA, regions]);
  const regionPaths = useMemo(() => svgPaths(regions.features, bounds), [regions, bounds]);
  const gridPaths = useMemo(() => svgPaths(grid.features, bounds), [grid, bounds]);
  const layerPaths = useMemo(() => svgPaths(layerA.features, bounds), [layerA, bounds]);
  const [staticZoom, setStaticZoom] = useState(1);
  const [staticCenter, setStaticCenter] = useState({ x: 500, y: 340 });
  const viewBox = useMemo(() => staticMapViewBox(staticZoom, staticCenter), [staticCenter, staticZoom]);
  const moveStaticMap = useCallback((dx, dy) => {
    setStaticCenter((current) => clampStaticCenter({ x: current.x + dx / staticZoom, y: current.y + dy / staticZoom }));
  }, [staticZoom]);
  const fitStaticMap = useCallback(() => {
    setStaticZoom(1);
    setStaticCenter({ x: 500, y: 340 });
  }, []);

  return (
    <div className={`static-map ${cleanMode ? "is-clean" : ""}`}>
      <svg viewBox={viewBox} role="img" aria-label={t("stage1TestArea")}>
        <rect width="1000" height="680" fill="#dfe6db" />
        {!cleanMode && (
          <g>
            {layerPaths.map((path, index) => (
              <path
                className={`layer-path ${path.quality === "public_wms_derived" ? "derived" : ""}`}
                d={path.d}
                key={`layer-${index}`}
              />
            ))}
          </g>
        )}
        <g>
          {gridPaths.map((path, index) => (
            <path
              aria-label={path.uuid ? `Select ${cellLabel(path.uuid)}` : undefined}
              className={`grid-path status-${path.status} ${
                path.uuid === selectedCellUuid ? "is-selected" : ""
              }`}
              d={path.d}
              key={`grid-${index}`}
              onClick={() => path.uuid && onSelectCell?.(path.uuid)}
              onKeyDown={(event) => {
                if (path.uuid && (event.key === "Enter" || event.key === " ")) {
                  event.preventDefault();
                  onSelectCell?.(path.uuid);
                }
              }}
              role={path.uuid ? "button" : undefined}
              tabIndex={path.uuid ? 0 : undefined}
            />
          ))}
        </g>
        <g>
          {regionPaths.map((path, index) => (
            <path className="region-path" d={path.d} key={`region-${index}`} />
          ))}
        </g>
      </svg>
      <div className="map-note">
        <MapPinned size={18} />
        <span>{displayNote}</span>
      </div>
      <button
        className="map-reset-button"
        onClick={fitStaticMap}
        type="button"
      >
        <Crosshair size={15} />
        <span>{t("fitArea")}</span>
      </button>
      <div className="map-zoom-controls" aria-label={t("mapZoomControls")}>
        <button
          aria-label={t("zoomIn")}
          onClick={() => setStaticZoom((current) => Math.min(3.8, current * MANUAL_ZOOM_STEP))}
          type="button"
        >
          +
        </button>
        <button
          aria-label={t("zoomOut")}
          onClick={() => setStaticZoom((current) => Math.max(1, current / MANUAL_ZOOM_STEP))}
          type="button"
        >
          -
        </button>
      </div>
      <div className="map-pan-controls" aria-label={t("mapPanControls")}>
        <button aria-label={t("panUp")} onClick={() => moveStaticMap(0, -90)} type="button">↑</button>
        <button aria-label={t("panLeft")} onClick={() => moveStaticMap(-90, 0)} type="button">←</button>
        <button aria-label={t("panRight")} onClick={() => moveStaticMap(90, 0)} type="button">→</button>
        <button aria-label={t("panDown")} onClick={() => moveStaticMap(0, 90)} type="button">↓</button>
      </div>
      <MapScopeBadge filtered={filtered} scope={scope} shownCells={shownCells ?? grid.features.length} summary={summary} />
      {selectedResult && <MapCellPopup onClearSelection={onClearSelection} row={selectedResult} />}
      <MapLegend cleanMode={cleanMode} dimmed={filtered && !grid.features.length} />
    </div>
  );
}


function staticMapViewBox(zoom, center) {
  const safeZoom = Math.max(1, Number(zoom) || 1);
  const width = 1000 / safeZoom;
  const height = 680 / safeZoom;
  const safeCenter = clampStaticCenter(center || { x: 500, y: 340 });
  const x = Math.min(1000 - width, Math.max(0, safeCenter.x - width / 2));
  const y = Math.min(680 - height, Math.max(0, safeCenter.y - height / 2));
  return `${x} ${y} ${width} ${height}`;
}

function clampStaticCenter(center) {
  return {
    x: Math.min(1000, Math.max(0, center.x)),
    y: Math.min(680, Math.max(0, center.y)),
  };
}

function mapRenderStats(map, hasSelectedFeature = true, expectedGridFeatures = null) {
  if (!ENABLE_MAP_STATS) {
    return {};
  }
  const center = map.getCenter();
  const staticGridRendered = map.getLayer("grid-fill")
    ? map.queryRenderedFeatures({ layers: ["grid-fill"] }).length
    : 0;
  const vectorGridRendered = map.getLayer("stage2-grid-tile-fill")
    ? map.queryRenderedFeatures({ layers: ["stage2-grid-tile-fill"] }).length
    : 0;
  const gridRendered = vectorGridRendered || staticGridRendered;
  const maskLayers = ["stage2-mask-country-fill", "stage2-mask-overview-fill"]
    .filter((id) => map.getLayer(id));
  const maskRendered = maskLayers.length
    ? map.queryRenderedFeatures({ layers: maskLayers }).length
    : 0;
  const selectedRendered = hasSelectedFeature
    ? map.getLayer("selected-cell-outline")
      ? map.queryRenderedFeatures({ layers: ["selected-cell-outline"] }).length
      : 0
    : 0;
  const vectorGridSourceFeatures = map.getSource("stage2-grid-tiles")
    ? map.querySourceFeatures("stage2-grid-tiles", { sourceLayer: "grid" }).length
    : 0;
  const staticGridSourceFeatures = map.getSource("grid") ? map.querySourceFeatures("grid").length : 0;
  const actualGridSourceFeatures = vectorGridSourceFeatures || staticGridSourceFeatures;
  const visibleGridFeatures = actualGridSourceFeatures || gridRendered;
  const selectedSourceFeatures = map.getSource("selected-cell")
    ? map.querySourceFeatures("selected-cell").length
    : 0;
  const maskSourceFeatures = ["stage2-mask-country-tiles", "stage2-mask-tiles"]
    .reduce((count, source) => count + (map.getSource(source)
      ? map.querySourceFeatures(source, { sourceLayer: "mask" }).length : 0), 0);
  return {
    center: [center.lng, center.lat],
    expectedGridFeatures,
    layerCFeatures: visibleGridFeatures,
    sourceGridFeatures: actualGridSourceFeatures,
    vectorGridSourceFeatures,
    actualRenderedGridFeatures: gridRendered,
    renderedGridFeatures: gridRendered,
    maskRenderedFeatures: maskRendered,
    maskSourceFeatures,
    renderedSelectedFeatures: hasSelectedFeature
      ? selectedRendered || selectedSourceFeatures
      : 0,
    zoom: map.getZoom(),
  };
}

function syncMapLayerVisibility(map, cleanMode = false, showSourceContext = false, scope, hasActiveFilters = false) {
  const layerCVisible = ENABLE_VECTOR_TILES && !cleanMode && scope?.key !== FULL_PUBLIC_SCOPE_KEY;
  setLayerVisibility(map, ["stage2-mask-overview-fill", "stage2-mask-overview-outline"], ENABLE_VECTOR_TILES && !hasActiveFilters);
  setLayerVisibility(map, ["stage2-mask-country-fill", "stage2-mask-country-outline"], ENABLE_VECTOR_TILES && !hasActiveFilters);
  setLayerVisibility(map, ["stage2-grid-tile-fill", "stage2-grid-tile-outline"], ENABLE_VECTOR_TILES);
  setLayerVisibility(map, ["stage2-layer-a-tile-fill", "stage2-layer-a-tile-outline"], ENABLE_VECTOR_TILES && showSourceContext);
  setLayerVisibility(map, ["stage2-layer-b-tile-fill"], ENABLE_VECTOR_TILES);
  setLayerVisibility(map, ["stage2-layer-c-tile-fill", "stage2-layer-c-tile-outline"], layerCVisible);
  setLayerVisibility(map, ["grid-fill", "grid-outline"], !ENABLE_VECTOR_TILES || hasActiveFilters);
  setLayerVisibility(map, ["layer-b-fill"], !ENABLE_VECTOR_TILES);
  setLayerVisibility(map, ["layer-a-fill", "layer-a-outline"], !ENABLE_VECTOR_TILES && showSourceContext);
}

function syncMapLayerZoomRanges(map, scope) {
  const cellMinZoom = stage2CellTileMinZoom(scope);
  setLayerZoomRangeIfLayer(map, "stage2-mask-overview-fill", COUNTRY_MASK_MAX_ZOOM, cellMinZoom);
  setLayerZoomRangeIfLayer(map, "stage2-mask-overview-outline", MASK_OUTLINE_MIN_ZOOM, cellMinZoom);
  setLayerZoomRangeIfLayer(map, "stage2-grid-tile-fill", cellMinZoom, MAX_TILE_RENDER_ZOOM);
  setLayerZoomRangeIfLayer(map, "stage2-grid-tile-outline", cellMinZoom, MAX_TILE_RENDER_ZOOM);
  setLayerZoomRangeIfLayer(map, "stage2-layer-b-tile-fill", cellMinZoom, MAX_TILE_RENDER_ZOOM);
  setLayerZoomRangeIfLayer(map, "stage2-layer-c-tile-fill", cellMinZoom, MAX_TILE_RENDER_ZOOM);
  setLayerZoomRangeIfLayer(map, "stage2-layer-c-tile-outline", cellMinZoom, MAX_TILE_RENDER_ZOOM);
}

function syncCountryMaskFallback(map, cellMinZoom, viewportPending, failedSources) {
  const zoom = map.getZoom();
  const nextSource = zoom >= cellMinZoom
    ? "stage2-grid-tiles"
    : zoom >= COUNTRY_MASK_MAX_ZOOM ? "stage2-mask-tiles" : null;
  const covered = nextSource && !viewportPending && !failedSources.has(nextSource)
    && map.getSource(nextSource) && map.isSourceLoaded(nextSource);
  setLayerVisibility(map, ["stage2-mask-country-fill", "stage2-mask-country-outline"],
    !nextSource || !covered);
  const source = nextSource || "stage2-mask-country-tiles";
  const error = failedSources.get(source) || "";
  return { source, error, pending: !error
    && (viewportPending || !map.getSource(source) || !map.isSourceLoaded(source)) };
}

function stage2CellTileMinZoom(scope) {
  return scope?.key === FULL_PUBLIC_SCOPE_KEY
    ? FULL_PUBLIC_CELL_TILE_MIN_ZOOM
    : DEFAULT_CELL_TILE_MIN_ZOOM;
}

function mapFocusBounds(regions, grid, layerA) {
  if (grid.features.length) {
    return featureBounds(grid.features);
  }
  if (regions.features.length) {
    return featureBounds(regions.features);
  }
  return featureBounds(layerA.features);
}

function refreshVectorTileSources(map, query) {
  if (!ENABLE_VECTOR_TILES) {
    return;
  }
  const sources = {
    "stage2-grid-tiles": tileUrl("grid", query),
    "stage2-mask-tiles": tileUrl("mask", query),
    "stage2-mask-country-tiles": tileUrl("mask", query),
    "stage2-layer-a-tiles": tileUrl("layer-a", query),
    "stage2-layer-b-tiles": tileUrl("layer-b", query),
    "stage2-layer-c-tiles": tileUrl("layer-c", query),
  };
  Object.entries(sources).forEach(([sourceId, url]) => {
    const source = map.getSource(sourceId);
    if (source && typeof source.setTiles === "function") {
      source.setTiles([url]);
    }
  });
  map.triggerRepaint();
}

function setLayerVisibility(map, layerIds, visible) {
  layerIds.forEach((layerId) => {
    if (map.getLayer(layerId)
      && (map.getLayoutProperty(layerId, "visibility") || "visible") !== (visible ? "visible" : "none")) {
      map.setLayoutProperty(layerId, "visibility", visible ? "visible" : "none");
    }
  });
}

function setLayerZoomRangeIfLayer(map, layerId, minzoom, maxzoom) {
  const layer = map.getLayer(layerId);
  if (layer && ((layer.minzoom ?? 0) !== minzoom || (layer.maxzoom ?? 24) !== maxzoom)) {
    map.setLayerZoomRange(layerId, minzoom, maxzoom);
  }
}

function safeResize(map) {
  try {
    map.resize();
  } catch {
    // Mapbox can briefly race its internal canvas during style/layout changes.
  }
}

function renderedFeaturesAt(map, point, layerIds) {
  const availableLayers = layerIds.filter((layerId) => map.getLayer(layerId));
  if (!availableLayers.length) {
    return [];
  }
  try {
    return map.queryRenderedFeatures(point, { layers: availableLayers });
  } catch {
    return [];
  }
}

function selectedCellFitOptions(map) {
  const canvas = map.getCanvas?.();
  const width = canvas?.clientWidth || window.innerWidth;
  return {
    minZoom: SELECTED_CELL_MIN_ZOOM,
    maxZoom: SELECTED_CELL_MAX_ZOOM,
    padding: width < 640 ? SELECTED_CELL_COMPACT_PADDING : SELECTED_CELL_PADDING,
  };
}

function projectFitOptions(isPreview = false) {
  return {
    maxZoom: isPreview ? 15 : PROJECT_FIT_MAX_ZOOM,
    padding: PROJECT_FIT_PADDING,
  };
}

function syncBaseMapContextVisibility(map, cleanMode = false) {
  const contextPattern = /(road|street|building|settlement|admin|place|poi|transit|airport|bridge|tunnel|ferry|route)/i;
  map.getStyle().layers?.forEach((layer) => {
    if (layer.id.startsWith("stage2-") || layer.id.startsWith("grid") || layer.id.startsWith("layer-")) {
      return;
    }
    if (layer.id.startsWith("selected-cell") || layer.id === "region-outline") {
      return;
    }
    const sourceLayer = String(layer["source-layer"] || "");
    if (layer.type === "symbol" || contextPattern.test(layer.id) || contextPattern.test(sourceLayer)) {
      map.setLayoutProperty(layer.id, "visibility", cleanMode ? "none" : "visible");
    }
  });
}

function syncModePaint(map, cleanMode = false, isPreview = false) {
  setPaintIfLayer(map, "grid-fill", "fill-color", gridStatusFillColorExpression(cleanMode, "verificationStatus"));
  setPaintIfLayer(map, "grid-fill", "fill-opacity", gridFillOpacityExpression(cleanMode, "verificationStatus"));
  setPaintIfLayer(map, "grid-outline", "line-color", gridStatusLineColorExpression(cleanMode, "verificationStatus"));
  setPaintIfLayer(map, "grid-outline", "line-opacity", cleanMode ? 0.86 : 0.78);
  setPaintIfLayer(map, "layer-b-fill", "fill-opacity", layerBFillOpacity(cleanMode, 0.2));
  setPaintIfLayer(map, "stage2-mask-overview-fill", "fill-color", MASK_OVERVIEW_COLOR);
  setPaintIfLayer(map, "stage2-mask-overview-outline", "line-color", MASK_OVERVIEW_LINE_COLOR);
  setPaintIfLayer(map, "stage2-grid-tile-fill", "fill-color", gridStatusFillColorExpression(cleanMode, "status"));
  setPaintIfLayer(map, "stage2-grid-tile-fill", "fill-opacity", isPreview ? 0.22 : stage2GridFillOpacity(cleanMode));
  setPaintIfLayer(map, "stage2-grid-tile-outline", "line-color", gridStatusLineColorExpression(cleanMode, "status"));
  setPaintIfLayer(map, "stage2-grid-tile-outline", "line-width", isPreview ? 1.4 : 0.8);
  setPaintIfLayer(map, "stage2-grid-tile-outline", "line-opacity", isPreview ? 0.9 : cleanMode ? 0.86 : 0.45);
  setPaintIfLayer(map, "stage2-layer-b-tile-fill", "fill-opacity", layerBFillOpacity(cleanMode, 0.22));
  setPaintIfLayer(map, "layer-a-fill", "fill-color", SOURCE_CONTEXT_COLOR);
  setPaintIfLayer(map, "layer-a-fill", "fill-opacity", SOURCE_CONTEXT_FILL_OPACITY);
  setPaintIfLayer(map, "layer-a-outline", "line-color", SOURCE_CONTEXT_COLOR);
  setPaintIfLayer(map, "layer-a-outline", "line-opacity", SOURCE_CONTEXT_LINE_OPACITY);
  setPaintIfLayer(map, "stage2-layer-a-tile-fill", "fill-color", SOURCE_CONTEXT_COLOR);
  setPaintIfLayer(map, "stage2-layer-a-tile-fill", "fill-opacity", SOURCE_CONTEXT_FILL_OPACITY);
  setPaintIfLayer(map, "stage2-layer-a-tile-outline", "line-color", SOURCE_CONTEXT_COLOR);
  setPaintIfLayer(map, "stage2-layer-a-tile-outline", "line-opacity", SOURCE_CONTEXT_LINE_OPACITY);
  setPaintIfLayer(map, "region-outline", "line-color", cleanMode ? "#e8f5ed" : "#1f2937");
  setPaintIfLayer(map, "region-outline", "line-width", cleanMode ? 1.4 : 2.2);
  setPaintIfLayer(map, "region-outline", "line-opacity", cleanMode ? 0.42 : 0.8);
}

function setPaintIfLayer(map, layerId, property, value) {
  if (map.getLayer(layerId)) {
    map.setPaintProperty(layerId, property, value);
  }
}

function gridStatusFillColorExpression(cleanMode, propertyName) {
  if (cleanMode) {
    return [
      "match",
      ["get", propertyName],
      "anomaly",
      "#d34a42",
      "watch",
      "#d0a231",
      "insufficient_data",
      "#718096",
      "unverified",
      "#718096",
      "#8ccaa6",
    ];
  }
  return verificationColorExpression(propertyName);
}

function gridStatusLineColorExpression(cleanMode, propertyName) {
  if (cleanMode) {
    return [
      "match",
      ["get", propertyName],
      "anomaly",
      "#f87171",
      "watch",
      "#f2c94c",
      "insufficient_data",
      "#718096",
      "unverified",
      "#718096",
      "#cce3d6",
    ];
  }
  return verificationColorExpression(propertyName);
}

function gridFillOpacityExpression(cleanMode, propertyName) {
  if (cleanMode) {
    return [
      "match",
      ["get", propertyName],
      "anomaly",
      0.2,
      "watch",
      0.14,
      "insufficient_data",
      0.12,
      "unverified",
      0.08,
      0.055,
    ];
  }
  return [
    "match",
    ["get", propertyName],
    "ok",
    0.16,
    "watch",
    0.24,
    "anomaly",
    0.3,
    "insufficient_data",
    0.12,
    0.08,
  ];
}

function stage2GridFillOpacity(cleanMode) {
  return cleanMode ? 0.06 : 0.07;
}

function layerBFillOpacity(cleanMode, maxOpacity) {
  return ["case", ["has", "ndvi"], cleanMode ? Math.min(maxOpacity, 0.16) : maxOpacity, 0];
}

function readMapStats() {
  return ENABLE_MAP_STATS ? window.__map1MapStats || {} : {};
}

function writeMapStats(stats) {
  if (ENABLE_MAP_STATS) {
    window.__map1MapStats = stats;
  }
}

function MapScopeBadge({ filtered = false, scope, shownCells = null, summary }) {
  const { t } = useI18n();
  const totalCells = Number(summary?.grid_cells || 0);
  const visibleCells = Number(shownCells ?? totalCells);
  const noun = t(scopeTextKey(scope?.cellNoun || "cells"), { defaultValue: scope?.cellNoun || "cells" });
  const cellText =
    filtered && totalCells && visibleCells !== totalCells
      ? t("shownCells", { shown: formatInteger(visibleCells), total: formatInteger(totalCells), noun })
      : `${formatInteger(totalCells || visibleCells)} ${noun}`;
  const label = t(scopeTextKey(scope?.badgeLabel), { defaultValue: scope?.badgeLabel || t("workspaceCoverage") });
  const description = t("activeWorkspaceDescription", { text: cellText });
  return (
    <div className="map-scope-badge" aria-label={`${label}: ${description}`}>
      <strong>{label}</strong>
      <span>{description}</span>
    </div>
  );
}

function MapLegend({ cleanMode = false, dimmed = false, showSourceContext = false }) {
  const { t } = useI18n();
  if (cleanMode) {
    return (
      <div className={`map-legend ${dimmed ? "is-dimmed" : ""}`}>
        <LegendItem label={t("activeClippedAssets")} status="active_asset" />
        <LegendItem label={t("selectedAsset")} status="selected" />
        <LegendItem label={t("ndviSignal")} status="ndvi" />
      </div>
    );
  }
  return (
    <div className={`map-legend ${dimmed ? "is-dimmed" : ""}`}>
      {showSourceContext && <LegendItem label={t("sourceContext")} status="source_context" />}
      <LegendItem label={t("ndviSignal")} status="ndvi" />
      <LegendItem label="OK" status="ok" />
      <LegendItem label={t("watch")} status="watch" />
      <LegendItem label={t("anomaly")} status="anomaly" />
      <LegendItem label={t("status.insufficientData")} status="insufficient_data" />
      <LegendItem label={t("noResult")} status="unverified" />
    </div>
  );
}

function countLayerBFeatures(grid) {
  return grid.features.filter((feature) => feature.properties?.ndvi !== null && feature.properties?.ndvi !== undefined)
    .length;
}

function mapFeatureCount(grid, visibleCellCount) {
  if (!ENABLE_VECTOR_TILES) {
    return grid.features.length;
  }
  const count = Number(visibleCellCount);
  return Number.isFinite(count) && count >= 0 ? count : grid.features.length;
}

function verificationColorExpression(propertyName) {
  return [
    "match",
    ["get", propertyName],
    "ok",
    "#2f855a",
    "watch",
    "#b7791f",
    "anomaly",
    "#c53030",
    "insufficient_data",
    "#718096",
    "#2f80ed",
  ];
}

function ndviColorExpression(_propertyName) {
  // The showcase does not publish observation breakpoints or classify risk.
  return "#4f8b80";
}

function MapCellPopup({ onClearSelection, row }) {
  const { t } = useI18n();
  return (
    <aside className="map-cell-popup" aria-label={t("selectedCellPopup")}>
      <div className="map-cell-popup-header">
        <span>{t("selectedAsset")}</span>
        {onClearSelection && (
          <button aria-label={t("clearSelectedAsset")} onClick={onClearSelection} type="button">
            <X size={14} />
          </button>
        )}
      </div>
      <strong>{cellLabel(row.cell_uuid)}</strong>
      <dl>
        <div>
          <dt>{t("evidence")}</dt>
          <dd>{statusLabel(row.anomaly_type, t)}</dd>
        </div>
        <div>
          <dt>{t("carbon")}</dt>
          <dd>{carbonReadinessLabel(row, t)}</dd>
        </div>
        <div>
          <dt>{t("area")}</dt>
          <dd>{row.area_ha ? `${formatNumber(row.area_ha)} ha` : t("dataUnavailable")}</dd>
        </div>
        <div>
          <dt>{t("signal")}</dt>
          <dd>NDVI {formatNumber(row.ndvi)}</dd>
        </div>
      </dl>
    </aside>
  );
}

function carbonReadinessLabel(row, t) {
  const status =
    row.carbon_total_potential_status ||
    row.carbon_readiness?.total_potential?.status ||
    row.carbon_readiness?.calculation_state ||
    row.carbon_calculation_status ||
    row.carbon_readiness?.calculation_status;
  const coefficientStatus = row.carbon_readiness?.coefficient_status || row.carbon_coefficient_status;
  if (row.carbon_readiness?.evidence_state && row.carbon_readiness.evidence_state !== "ready") {
    return t("awaitingEvidence");
  }
  if (!status || status === "future_analytics_module" || status === "pending_coefficients") {
    return t("awaitingCoefficients");
  }
  if (status === "baseline_estimate" || (status === "calculated" && coefficientStatus === "baseline")) {
    return t("baselineEstimate");
  }
  if (status === "draft_estimate" || (status === "calculated" && coefficientStatus === "draft")) {
    return t("draftEstimate");
  }
  return {
    calculated: t("anyCalculatedValue"),
    failed: t("failed"),
    pending_coefficients: t("awaitingCoefficients"),
    pending_inputs: t("pendingInputs"),
    skipped_missing_evidence: t("missingEvidence"),
  }[status] || String(status).replaceAll("_", " ");
}

function LegendItem({ label, status }) {
  return (
    <span>
      <i className={`legend-dot status-${status}`} />
      {label}
    </span>
  );
}

function mapNotice({ error, hasActiveFilters, hasMatchingResults, loading, mapError = false, mapReady = false, noToken = false, t }) {
  if (error) {
    return t("dataUnavailable");
  }
  if (hasActiveFilters && !hasMatchingResults && !loading) {
    return t("noMatchingCells");
  }
  if (mapError || noToken) {
    return t("mapPreviewActive");
  }
  if (!mapReady) {
    return t("mapLoading");
  }
  return t("operationalMapActive");
}

function scopeTextKey(value) {
  return `scope.${String(value || "").trim().replaceAll(/\s+/g, ".").toLowerCase()}`;
}

function absoluteApiBaseUrl(value) {
  if (/^https?:\/\//i.test(value)) {
    return value;
  }
  if (typeof window === "undefined") {
    return value;
  }
  return new URL(value, window.location.origin).toString();
}
