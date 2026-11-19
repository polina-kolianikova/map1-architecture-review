import { appendQuery, fetchJson } from "./stage1";
import { scopeSearchParams } from "../config/scopes";

function stage2Params(scope, params = new URLSearchParams()) {
  const merged = scopeSearchParams(scope);
  params.forEach((value, key) => {
    merged.set(key, value);
  });
  return merged;
}

export async function fetchAsset(cellUuid, scope) {
  return fetchJson(appendQuery(`/stage2/assets/${encodeURIComponent(cellUuid)}`, scopeSearchParams(scope)));
}

export async function pickAssetAtLocation({ lng, lat, scope }) {
  const params = new URLSearchParams({
    lat: String(lat),
    lng: String(lng),
  });
  return fetchJson(appendQuery("/stage2/assets/pick", stage2Params(scope, params)));
}

export async function fetchAssetNdviHistory(cellUuid, months = 12, scope) {
  const params = new URLSearchParams({ months: String(months) });
  return fetchJson(
    appendQuery(`/stage2/assets/${encodeURIComponent(cellUuid)}/ndvi-history`, stage2Params(scope, params))
  );
}

export async function fetchAssetPage({
  carbonStatus = "all",
  limit = 100,
  offset = 0,
  observedFrom = "",
  observedTo = "",
  search = "",
  signalStatus = "all",
  status = "all",
  scope,
} = {}) {
  const params = new URLSearchParams({
    carbon_status: carbonStatus,
    limit: String(limit),
    observed_from: observedFrom,
    observed_to: observedTo,
    offset: String(offset),
    search,
    signal_status: signalStatus,
    status,
  });
  return fetchJson(appendQuery("/stage2/assets", stage2Params(scope, params)));
}

export async function fetchAssetIds({
  carbonStatus = "all",
  limit = 10000,
  observedFrom = "",
  observedTo = "",
  search = "",
  signalStatus = "all",
  status = "all",
  scope,
} = {}) {
  const params = new URLSearchParams({
    carbon_status: carbonStatus,
    limit: String(limit),
    observed_from: observedFrom,
    observed_to: observedTo,
    search,
    signal_status: signalStatus,
    status,
  });
  return fetchJson(appendQuery("/stage2/assets/ids", stage2Params(scope, params)));
}
