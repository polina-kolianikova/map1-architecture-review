import { API_BASE_URL, API_CREDENTIALS_MODE, API_TIMEOUT_MS } from "../config/runtime";
import { scopeSearchParams } from "../config/scopes";

export async function fetchJson(path, { timeoutMs = API_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      credentials: API_CREDENTIALS_MODE,
      signal: controller.signal,
    });
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error(`${path} timed out`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
  if (!response.ok) {
    if (response.status === 501) {
      throw new Error("Service omitted from architecture sample (HTTP 501)");
    }
    throw new Error(`${path} returned ${response.status}`);
  }
  return response.json();
}

export async function fetchAllPages(path, limit = 500, maxRows = 25000) {
  const rows = [];
  let offset = 0;

  while (true) {
    if (rows.length >= maxRows) {
      return { rows, truncated: true };
    }
    const separator = path.includes("?") ? "&" : "?";
    const page = await fetchJson(`${path}${separator}limit=${limit}&offset=${offset}`);
    rows.push(...page);
    if (page.length < limit) {
      return { rows, truncated: false };
    }
    offset += limit;
  }
}

export function appendQuery(path, params) {
  const query = String(params?.toString?.() || params || "").replace(/^\?/, "");
  if (!query) {
    return path;
  }
  return `${path}${path.includes("?") ? "&" : "?"}${query}`;
}

export function scopedPath(path, scope) {
  return appendQuery(path, scopeSearchParams(scope));
}
