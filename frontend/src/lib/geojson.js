import { TEST_BOUNDS } from "../config/runtime";

export function toFeatureCollection(items, properties) {
  return {
    type: "FeatureCollection",
    features: items
      .filter((item) => item.geometry)
      .map((item) => ({
        type: "Feature",
        geometry: item.geometry,
        properties: properties(item),
      })),
  };
}

export function featureBounds(features) {
  const coordinates = features.flatMap((feature) => flattenCoordinates(feature.geometry?.coordinates || []));
  if (!coordinates.length) {
    return TEST_BOUNDS;
  }
  const longitudes = coordinates.map((point) => point[0]);
  const latitudes = coordinates.map((point) => point[1]);
  return [
    Math.min(...longitudes),
    Math.min(...latitudes),
    Math.max(...longitudes),
    Math.max(...latitudes),
  ];
}

export function svgPaths(features, bounds) {
  return features
    .map((feature) => {
      const rings = geometryRings(feature.geometry);
      const path = rings
        .map((ring) =>
          ring.map((point, index) => `${index === 0 ? "M" : "L"} ${project(point, bounds)}`).join(" ")
        )
        .join(" Z ");
      return {
        d: path ? `${path} Z` : "",
        quality: feature.properties?.quality,
        status: feature.properties?.verificationStatus || "unverified",
        uuid: feature.properties?.uuid,
      };
    })
    .filter((item) => item.d);
}

export function selectedFeatureCollection(grid, selectedCellUuid) {
  if (!selectedCellUuid) {
    return { type: "FeatureCollection", features: [] };
  }
  return {
    type: "FeatureCollection",
    features: grid.features.filter((feature) => feature.properties?.uuid === selectedCellUuid),
  };
}

function flattenCoordinates(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  if (typeof value[0] === "number" && typeof value[1] === "number") {
    return [value];
  }
  return value.flatMap(flattenCoordinates);
}

function geometryRings(geometry) {
  if (!geometry?.coordinates) {
    return [];
  }
  if (geometry.type === "Polygon") {
    return geometry.coordinates;
  }
  if (geometry.type === "MultiPolygon") {
    return geometry.coordinates.flat();
  }
  return [];
}

function project(point, bounds) {
  const [minLon, minLat, maxLon, maxLat] = bounds;
  const [lon, lat] = point;
  const padding = 54;
  const width = 1000 - padding * 2;
  const height = 680 - padding * 2;
  const x = padding + ((lon - minLon) / Math.max(maxLon - minLon, 0.000001)) * width;
  const y = padding + ((maxLat - lat) / Math.max(maxLat - minLat, 0.000001)) * height;
  return `${x.toFixed(1)} ${y.toFixed(1)}`;
}

export function fitMapToBounds(map, bounds, options = {}) {
  if (!map) {
    return;
  }
  const [minLon, minLat, maxLon, maxLat] = bounds;
  const duration = options.duration ?? 0;
  const maxZoom = options.maxZoom ?? 13;
  map.fitBounds(
    [
      [minLon, minLat],
      [maxLon, maxLat],
    ],
    {
      padding: options.padding ?? 72,
      duration,
      maxZoom,
    }
  );
  if (!Number.isFinite(options.minZoom)) {
    return;
  }

  const minZoom = Math.min(options.minZoom, maxZoom);
  const center = [(minLon + maxLon) / 2, (minLat + maxLat) / 2];
  const tightenZoom = () => {
    if (map.getZoom() >= minZoom) {
      return;
    }
    map.easeTo({
      center,
      zoom: minZoom,
      duration: Math.min(duration, 220),
    });
  };

  if (duration > 0) {
    map.once("moveend", tightenZoom);
    window.requestAnimationFrame(tightenZoom);
    window.setTimeout(tightenZoom, duration + 80);
  } else {
    tightenZoom();
  }
}
