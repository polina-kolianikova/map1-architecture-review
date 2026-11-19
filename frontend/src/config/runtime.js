const RUNTIME_CONFIG = typeof window === "undefined" ? {} : window.__MAP1_CONFIG__ || {};
const IMPORT_META_ENV = import.meta.env || {};

export const API_BASE_URL =
  RUNTIME_CONFIG.apiBaseUrl || IMPORT_META_ENV.VITE_API_BASE_URL || "/api";
export const API_CREDENTIALS_MODE =
  RUNTIME_CONFIG.apiCredentials || IMPORT_META_ENV.VITE_API_CREDENTIALS || "same-origin";
export const APP_LOCALE =
  RUNTIME_CONFIG.locale || IMPORT_META_ENV.VITE_APP_LOCALE || "en";
export const APP_AVAILABLE_LOCALES =
  listValue(RUNTIME_CONFIG.availableLocales || IMPORT_META_ENV.VITE_APP_AVAILABLE_LOCALES);
export const ENABLE_MAP_STATS =
  isEnabled(RUNTIME_CONFIG.enableMapStats) ||
  IMPORT_META_ENV.VITE_ENABLE_MAP_STATS === "1" ||
  (typeof window !== "undefined" && window.__MAP1_ENABLE_TEST_STATS__ === true);
export const ENABLE_VECTOR_TILES =
  isEnabled(RUNTIME_CONFIG.enableVectorTiles) ||
  IMPORT_META_ENV.VITE_ENABLE_VECTOR_TILES === "1";
export const MAPBOX_TOKEN =
  RUNTIME_CONFIG.mapboxToken || IMPORT_META_ENV.VITE_MAPBOX_TOKEN || "";
export const FULL_PUBLIC_REGION_CODE =
  scopeIdentity(RUNTIME_CONFIG.fullPublicRegionCode) ||
  scopeIdentity(IMPORT_META_ENV.VITE_FULL_PUBLIC_REGION_CODE) ||
  "REVIEW-SCOPE";
export const FULL_PUBLIC_GRID_VERSION =
  scopeIdentity(RUNTIME_CONFIG.fullPublicGridVersion) ||
  scopeIdentity(IMPORT_META_ENV.VITE_FULL_PUBLIC_GRID_VERSION) ||
  "review-grid";

export const API_TIMEOUT_MS = 15000;
export const VERIFICATION_PAGE_SIZE = 12;
// Generic viewport only, not a dataset or operational region.
export const TEST_BOUNDS = [-1, -1, 1, 1];
export const TEST_CENTER = [
  (TEST_BOUNDS[0] + TEST_BOUNDS[2]) / 2,
  (TEST_BOUNDS[1] + TEST_BOUNDS[3]) / 2,
];

function isEnabled(value) {
  return value === true || value === "true" || value === "1";
}

function scopeIdentity(value) {
  if (typeof value !== "string") {
    return "";
  }
  const identity = value.trim();
  return identity.length > 0 && identity.length <= 120 ? identity : "";
}

function listValue(value) {
  if (Array.isArray(value)) {
    return value;
  }
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}
