import { FULL_PUBLIC_GRID_VERSION, FULL_PUBLIC_REGION_CODE } from "./runtime.js";

export const PILOT_SCOPE_KEY = "pilot";
export const V9_PILOT_SCOPE_KEY = "v9-pilot";
export const FULL_PUBLIC_SCOPE_KEY = "full-public-rollout";

export const WORKSPACE_SCOPES = [
  {
    key: V9_PILOT_SCOPE_KEY,
    label: "Client clean pilot",
    shortLabel: "Client pilot",
    badgeLabel: "Client clean pilot",
    cellNoun: "pilot cells",
    sidebarTitle: "Pilot totals",
    signalTitle: "Pilot status",
    regionStatus: "Pilot QA",
    topbarChips: ["Clean mask", "Pilot QA"],
    globalMeta: [
      ["Workspace", "Client clean pilot"],
      ["Mask", "OGM and OSM clean forest"],
      ["Scale", "100 m grid"],
      ["Evidence state", "Pilot QA"],
    ],
    globalNote:
      "Optional scope boundary in this architecture sample. No pilot data or private rules are included.",
    regionCode: "REVIEW-PILOT",
    gridVersion: "review-pilot-grid",
    previewBounds: [-1, -1, 1, 1],
    isPreview: true,
  },
  {
    key: FULL_PUBLIC_SCOPE_KEY,
    label: "Public Forest Active Grid",
    shortLabel: "Active Grid",
    badgeLabel: "Active Grid",
    cellNoun: "public rollout cells",
    sidebarTitle: "Rollout totals",
    signalTitle: "Rollout status",
    regionStatus: "Active Grid",
    topbarChips: ["Public forest mask", "Forest-mask scope"],
    globalMeta: [
      ["Workspace", "Active Grid"],
      ["Land scope", "Public forest mask"],
      ["Scale", "Active Grid"],
      ["Evidence state", "Layer B / CO2"],
    ],
    globalNote:
      "Active Grid shows the current public forest-mask grid with Layer B satellite evidence and Stage 5 carbon status where public signal is available.",
    regionCode: FULL_PUBLIC_REGION_CODE,
    gridVersion: FULL_PUBLIC_GRID_VERSION,
    previewBounds: [-1, -1, 1, 1],
    isPreview: true,
  },
];

export const DEFAULT_WORKSPACE_SCOPE_KEY =
  import.meta.env?.VITE_DEFAULT_WORKSPACE_SCOPE_KEY || FULL_PUBLIC_SCOPE_KEY;

export function resolveWorkspaceScope(key) {
  return WORKSPACE_SCOPES.find((scope) => scope.key === key)
    || WORKSPACE_SCOPES.find((scope) => scope.key === FULL_PUBLIC_SCOPE_KEY);
}

export function scopeSearchParams(scope) {
  const params = new URLSearchParams();
  if (scope?.regionCode) {
    params.set("region_code", scope.regionCode);
  }
  if (scope?.gridVersion) {
    params.set("grid_version", scope.gridVersion);
  }
  return params;
}
