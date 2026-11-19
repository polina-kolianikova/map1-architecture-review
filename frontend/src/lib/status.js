import { formatDate } from "./formatters";

export function qualityLabel(value) {
  const t = arguments[1];
  return {
    public_ogm_rest: label(t, "quality.publicOgmRest", "Public OGM REST"),
    public_ogm_osm_clean_mask: label(t, "quality.publicForestMaskPreview", "Public forest mask"),
    public_osm_worldcover_clean_mask: label(t, "quality.publicForestMaskPreview", "Public forest mask"),
    public_preview: label(t, "quality.publicForestMaskPreview", "Public forest mask"),
    public_wms_derived: label(t, "quality.publicWmsDerived", "Public WMS-derived"),
  }[value] || value || "";
}

export function sourceCheckLabel(source) {
  const t = arguments[1];
  if (!source?.latest_check_status) {
    return label(t, "sourceCheck.pending", "Check pending");
  }
  if (source.latest_check_status === "ok") {
    return "OK";
  }
  if (source.latest_check_status === "unavailable" && Number(source.feature_count || 0) > 0) {
    return label(t, "sourceCheck.retained", "Last data retained");
  }
  if (source.latest_check_status === "unavailable") {
    return label(t, "sourceCheck.unavailable", "Source check unavailable");
  }
  return source.latest_check_status;
}

export function sourceFreshnessLabel(source) {
  const t = arguments[1];
  if (!source?.last_successful_refresh_at) {
    if (source?.official_status === "public_preview") {
      return label(t, "sourceFreshness.previewRetained", "forest mask retained");
    }
    return label(t, "sourceFreshness.noRetainedData", "No retained data");
  }
  const dataAge = Number(source.data_age_days);
  if (Number.isFinite(dataAge)) {
    return label(t, "sourceFreshness.ageDays", "data {{days}}d old", { days: dataAge });
  }
  return formatDate(source.last_successful_refresh_at);
}

export function sourceQualityWarning(dataQuality) {
  const t = arguments[1];
  if (!dataQuality || dataQuality.status === "ok") {
    return "";
  }
  return dataQuality.message || label(t, "sourceQuality.degraded", "Some public data sources are degraded.");
}

export function sourceCheckStatusLabel(value) {
  const t = arguments[1];
  return {
    ok: label(t, "sourceCheck.latestOk", "Latest check OK"),
    unavailable: label(t, "sourceCheck.latestUnavailable", "Latest check unavailable"),
  }[value] || value || label(t, "sourceCheck.none", "No source check");
}

export function sourceAgeLabel(value) {
  const t = arguments[1];
  if ((typeof value !== "number" && typeof value !== "string") || (typeof value === "string" && !value.trim())) {
    return "n/a";
  }
  const age = Number(value);
  if (!Number.isFinite(age)) {
    return "n/a";
  }
  return label(t, "sourceAge.days", "{{days}}d old", { days: age });
}

export function relationLabel(value) {
  const t = arguments[1];
  return {
    current_public_layer_a_intersection: label(t, "relation.currentLayerA", "Intersects current Layer A"),
    no_current_public_layer_a_intersection: label(t, "relation.noLayerA", "No current Layer A match"),
  }[value] || value || label(t, "relation.none", "No relation");
}

export function observationLabel(value) {
  const t = arguments[1];
  return {
    ok: label(t, "observation.ok", "Valid Sentinel NDVI"),
    no_ndvi: label(t, "observation.noNdvi", "No NDVI"),
    no_valid_pixels: label(t, "observation.noValidPixels", "No valid pixels"),
    not_observed: label(t, "observation.notObserved", "Awaiting observation"),
    request_failed: label(t, "observation.requestFailed", "Request failed"),
  }[value] || label(t, "observation.unknown", "Unknown Sentinel status");
}

export function statusLabel(value) {
  const t = arguments[1];
  return {
    ok: "OK",
    watch: label(t, "status.watch", "Watch"),
    anomaly: label(t, "status.anomaly", "Anomaly"),
    insufficient_data: label(t, "status.insufficientData", "Insufficient data"),
    unverified: label(t, "status.noResult", "No result"),
  }[value] || value || label(t, "status.noResult", "No result");
}

export function eventLabel(value) {
  const t = arguments[1];
  return {
    created: label(t, "event.created", "Created"),
    status_changed: label(t, "event.statusChanged", "Status changed"),
    status_recalculated: label(t, "event.statusRecalculated", "Recalculated"),
  }[value] || value || label(t, "event.auditEvent", "Audit event");
}

export function statusRank(status) {
  return {
    ok: 1,
    insufficient_data: 2,
    watch: 3,
    anomaly: 4,
  }[status] || 0;
}

function label(t, key, fallback, params = {}) {
  if (typeof t !== "function") {
    return fallback;
  }
  const value = t(key, params);
  return value && value !== key ? value : fallback;
}
