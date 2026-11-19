export const DEFAULT_LOCALE = "en";
export const FALLBACK_LOCALE = "en";

export const localeLabels = {
  en: "English",
};

export const localeAliases = {
  "en-gb": "en",
  "en-us": "en",
};
const LOCALE_PATTERN = /^[a-z]{2,3}(-[a-z0-9]{2,8})*$/;
const MAX_LOCALE_LENGTH = 35;

export const messages = {
  en: {
    pilotScopeUnavailable: "Pilot data are unavailable. Please use the full map.",
    pilotScopeFallback: "Pilot data are unavailable. Showing the full map.",
    assetPassport: "Asset passport",
    assetIdentity: "Asset identity",
    assetIdentityLoading: "Asset identity, verification evidence and carbon inputs are being loaded.",
    assetPassportEmptyCopy: "Asset identity, verification evidence and carbon input status appear here after selection.",
    assetRegister: "Asset Register",
    assetRegisterFilteredNotice:
      "Asset Register is filtered. The map stays in full workspace context because this result set exceeds the display limit.",
    assetStatus: "Asset status",
    assets: "Assets",
    assetRows: "Showing {{shown}} of {{total}} assets, page {{page}} of {{pageCount}}",
    assetRowsApprox: "{{count}} assets, page {{page}} of {{pageCount}}",
    auditBackedEvidence:
      "Operational GIS/MRV workspace for forest assets, satellite signal, verification status, and audit-backed evidence.",
    auditEvents: "Audit events",
    auditEvidence: "Audit evidence",
    assetAudit: "Asset audit",
    assetAuditBreakdown: "Verification: {{verification}} · Current carbon: {{carbon}}",
    verificationAudit: "Verification audit",
    lastVerificationEvent: "Last verification event",
    lastCarbonEvent: "Last carbon event",
    acquisition: "Acquisition",
    awaitingCoefficients: "Awaiting coefficients",
    awaitingEvidence: "Awaiting evidence",
    awaitingInputs: "Awaiting inputs",
    awaitingAnnualSlices: "Awaiting annual slices",
    boundedStage1Area: "Verified pilot area",
    biomassCarbon: "Biomass carbon",
    calibrationSource: "Calibration source",
    carbon: "Carbon",
    carbonAudit: "Current carbon audit",
    carbonReadiness: "Carbon readiness",
    carbonReadinessTitle: "Carbon Readiness",
    clear: "Clear",
    clean: "Clean",
    cleanMode: "Clean mode",
    cloud: "Cloud",
    coefficientPending: "Coefficient pending",
    coefficientSet: "Coefficient set",
    coefficientSource: "Coefficient source",
    calculationState: "Calculation state",
    confidence: "Confidence",
    confidenceScore: "Confidence score",
    carbonConfidence: "Carbon confidence",
    carbonConfidenceHint: "Based on input quality and available evidence.",
    context: "Context",
    contextMode: "Context mode",
    co2Signal: "CO2 signal",
    current: "Current",
    dataAge: "Data age",
    dataLoadFailed: "Could not load {{sources}}",
    dataQuality: "Data quality",
    dataQualityLabel: "Data quality",
    dataRefreshFailed: "Data refresh failed: {{error}}",
    dataUnavailable: "Data unavailable",
    retryLoading: "Retry",
    dashboardSummaryPreparing: "Preparing the dashboard summary. This page will update automatically.",
    summaryAsOf: "Summary as of {{time}}",
    dataUnavailableRefresh: "Data unavailable. Refresh to retry",
    dark: "Dark",
    evidence: "Evidence",
    evidenceLog: "Evidence log",
    evidenceStatus: "Evidence status",
    evidenceState: "Evidence state",
    exitFocus: "Exit focus",
    expansionTarget: "Expansion target",
    fitArea: "Fit area",
    focusMap: "Focus map",
    forestAssetIntelligence: "Forest asset command center",
    forestOnlyScope: "Forest-only scope",
    frameworkPrepared: "Framework prepared",
    futureNationalTarget: "Future national target: 23M ha",
    global: "Overview",
    grossIncrement: "Gross increment",
    gridCells: "Grid cells",
    inputSlotReady: "Input slot ready",
    layerARecords: "Layer A records",
    layerASource: "Layer A source",
    lastAudit: "Last audit",
    lastObservation: "Last observation",
    layerBBatches: "Layer B batches",
    latestEventCountInPassport: "Latest event count is available in the asset passport",
    loadingData: "Loading data",
    loadingNdviHistory: "Loading NDVI history",
    loadingSelectedAsset: "Loading selected asset",
    lossFactor: "Loss factor",
    mapLoading: "Map loading",
    mapPreviewActive: "Active Grid overview",
    meanAnnualRate: "Mean annual rate",
    methodologyPending: "Model pending",
    methodologyState: "Model state",
    methodologyVersion: "Model version",
    mode: "Mode",
    ndviCells: "NDVI cells",
    ndviCoverage: "NDVI coverage",
    ndviHistoryRange: "NDVI history range",
    ndviHistoryUnavailable: "NDVI history unavailable",
    ndviHistoryOneObservation:
      "One valid observation is available. A second observation is needed to calculate a trend.",
    ndviSignal: "NDVI signal",
    ndviTrend: "NDVI trend",
    netAnnualIncrement: "Net Annual Increment",
    noMatchingAssets: "No matching assets",
    noMatchingVerificationResults: "No matching verification results",
    noAssetAtLocation: "No asset at this map location",
    noAssetsAvailable: "No assets available",
    noAuditEventsCurrentAsset: "No audit events for the current asset",
    noAuditEventsYet: "No audit events yet",
    noLayerARecords: "No Layer A records loaded yet",
    noMatchingAssetSelected: "No matching asset selected",
    noMatchingCells: "No matching cells",
    noSentinelObservations: "No Sentinel observations loaded yet",
    noVerificationResultRecorded: "No verification result recorded for this asset yet.",
    noVerificationResultsYet: "No verification results yet",
    none: "none",
    operationalFallbackView: "Operational fallback view",
    operationalMapActive: "Operational map active",
    openTurkeyWorkspace: "Enter Turkey workspace",
    pending: "Pending",
    portfolioRegions: "Pilot workspace",
    previousAcquisition: "Previous acquisition",
    previousNdvi: "Previous NDVI",
    privateSystem: "Private GIS/MRV system",
    refresh: "Refresh",
    refreshing: "Refreshing",
    registry: "Registry",
    registryEvidence: "Registry evidence",
    registryMatch: "Registry match",
    registrySources: "Registry sources",
    relation: "Relation",
    regionStatusActive: "Active pilot",
    regionStatusReady: "Ready",
    rootSystem: "Root system",
    sampleCount: "Sample count",
    satelliteEvidence: "Satellite evidence",
    satelliteSignal: "Satellite signal",
    score: "Score {{score}}",
    verificationRiskScore: "Verification risk score {{score}}",
    verificationRiskScoreHint: "Higher means greater risk (0 to 1).",
    searchCell: "Search cell ID or UUID",
    selectAssetAudit: "Select an asset to view audit events",
    selectAssetPassport: "Select a grid cell or register row",
    selectedAsset: "Selected asset",
    selectedCellPopup: "Selected cell popup",
    selectedAssetSummary: "Selected asset summary",
    signal: "Signal",
    signalCover: "Signal cover",
    soilCarbon: "Soil carbon",
    sourceCheck: "Source check",
    sourceContext: "Source context, not asset",
    sourceQuality: "Source quality",
    statusRows: "Status rows",
    totalPotential: "Total Potential",
    uuid: "UUID",
    validPixels: "Valid pixels",
    verificationResults: "Verification results",
    verificationFactor: "Verification factor",
    verificationRows: "{{count}} rows, page {{page}} of {{pageCount}}",
    workspaceTotals: "Workspace totals",
    yoyComparison: "YoY comparison",
    annualAssetDelta: "Annual asset delta",
    annualAssetRate: "Annual asset rate",
    annualDeltaHa: "Annual delta /ha",
    activeClippedAssets: "Active clipped assets",
    activeWorkspaceDescription: "{{text}} in the active workspace.",
    activeWorkspaceSentence: "{{count}} {{noun}} in the active workspace.",
    area: "Area",
    areaCoefficient: "Area coefficient",
    asset: "Asset",
    assetDetailsUnavailable: "Asset details unavailable",
    assetRegisterPages: "Asset Register pages",
    assetRegisterUnavailable: "Asset Register unavailable",
    auditTransitionTo: "to",
    approvedCoefficientsLoaded: "Approved coefficients loaded",
    awaitingCurrentYear: "Awaiting current year",
    awaitingHistory: "Awaiting history",
    awaitingPreviousCarbonSnapshot: "Awaiting previous carbon snapshot",
    awaitingPreviousYear: "Awaiting previous year",
    awaitingSatelliteSignal: "Awaiting satellite signal",
    awaitingVerification: "Awaiting verification",
    baselineConfig: "Baseline config",
    baselineDraft: "Baseline Assessment",
    baselineEstimateFieldCalibrationPending: "Baseline estimate, field calibration pending.",
    calculated: "Calculated",
    carbonModelInputs: "Carbon model inputs",
    cellOverride: "Cell override",
    all: "All",
    anomaly: "Anomaly",
    anyCalculatedValue: "Any calculated value",
    awaitingCoefficientsOption: "Awaiting coefficients",
    baselineEstimate: "Baseline Assessment",
    carbonAuditEvents_one: "{{formattedCount}} event",
    carbonAuditEvents_other: "{{formattedCount}} events",
    cleanMask: "Clean mask",
    clearAssetPassport: "Clear asset passport",
    clearSelectedAsset: "Clear selected asset",
    configured: "Configured",
    configuredBaseline: "Configured baseline",
    coverage: "Coverage",
    currentPortfolioSnapshot: "Current portfolio snapshot",
    dashboard: "Dashboard",
    degraded: "Degraded",
    decliningTrend: "declining {{value}}",
    draft: "Baseline Assessment",
    draftEstimate: "Baseline Assessment",
    draftEstimatePendingCoefficientApproval: "Baseline assessment, pending coefficient approval.",
    evidenceFilter: "Evidence",
    failed: "Failed",
    fullPreview: "Active Grid",
    fullPreviewWorkspace: "Active Grid workspace",
    fullPublicForestRolloutPreview: "Public Forest Active Grid",
    globalBaseline: "Global baseline",
    hideSourceContext: "Hide source context",
    historicalObservationsNoNumeric:
      "Historical observations are present, but no numeric NDVI was recorded.",
    historicalObservationsPending:
      "Historical observations will appear after the next refresh cycle.",
    historicalObservationsWithoutValidNdvi:
      "Historical observations found, but no valid NDVI signal: {{statuses}}.",
    improvingTrend: "improving {{value}}",
    incrementModel: "Increment model",
    inputReady: "Input slot ready",
    layerA: "Layer A",
    layerDReserved: "Layer D reserved",
    loading: "Loading",
    mapPanControls: "Static map pan controls",
    mapZoomControls: "Map zoom controls",
    missingEvidence: "Missing evidence",
    next: "Next",
    noCellsMatchFilters: "No cells match the current filters. Context layers remain visible.",
    noExplanationRecorded: "No explanation recorded.",
    noIntersectingSource: "No intersecting source",
    noLayerAMatch: "No Layer A match",
    noMatch: "No match",
    noNdvi: "No NDVI",
    noResult: "No result",
    notAssigned: "Not assigned",
    noValidPixels: "No valid pixels",
    observedDateRangeInvalid: "Observed date range is invalid",
    observedDateRangeInvalidLong: "Observed date range is invalid. Set the start date before the end date.",
    observedFrom: "Observed from",
    observedTo: "Observed to",
    openWorkspace: "Open {{name}} workspace",
    panDown: "Pan down",
    panLeft: "Pan left",
    panRight: "Pan right",
    panUp: "Pan up",
    pendingCoefficients: "Pending coefficients",
    pendingInputs: "Pending inputs",
    pendingApproval: "Pending approval",
    portfolioOverview: "Portfolio overview",
    portfolioWorkflow: "Portfolio workflow",
    portfolioWorkspace: "Portfolio workspace",
    previous: "Prev",
    preview: "Active Grid",
    previewSummaryLoadFailed: "Could not load Active Grid summary",
    projectCoverageScope: "Project coverage scope",
    qualityOk: "OK",
    region: "Region",
    regionOverride: "Region override",
    reserved: "Reserved",
    riskModel: "Risk model",
    requestFailed: "Request failed",
    shortIdOrUuid: "Short ID or UUID",
    showSourceContext: "Show source context",
    shownCells: "{{shown}} shown of {{total}} {{noun}}",
    sourceLayerA: "Layer A source",
    stableTrend: "stable {{value}}",
    stage1TestArea: "Stage 1 test area",
    startDateBeforeEndDate: "Start date must be before end date.",
    sourceContextToggle: "Source context toggle",
    turkey: "Turkey",
    turkeyRegion: "Turkey region",
    turkeyPilotWorkspace: "Pilot forest asset workspace",
    unavailable: "Unavailable",
    unknown: "Unknown",
    validNdvi: "Valid NDVI",
    watch: "Watch",
    workspaceCoverage: "Workspace coverage",
    workspaceOperatingState: "Workspace operating state",
    workspaceScope: "Workspace scope",
    workspaceSignal: "Workspace signal",
    "scope.100.m.grid": "100 m grid",
    "scope.clean.mask": "Clean mask",
    "scope.client.clean.pilot": "Client clean pilot",
    "scope.client.pilot": "Client pilot",
    "scope.evidence.state": "Evidence state",
    "scope.forest-mask.scope": "Forest-mask scope",
    "scope.active.grid": "Active Grid",
    "scope.public.forest.active.grid": "Public Forest Active Grid",
    "scope.full.preview": "Active Grid",
    "scope.full.public.rollout.preview": "Public Forest Active Grid",
    "scope.land.scope": "Land scope",
    "scope.layer.b./.co2": "Layer B / CO2",
    "scope.mask": "Mask",
    "scope.ogm.and.osm.clean.forest": "OGM and OSM clean forest",
    "scope.pilot.cells": "pilot cells",
    "scope.pilot.qa": "Pilot QA",
    "scope.public.forest.mask": "Public forest mask",
    "scope.public.mask.preview": "Public forest mask",
    "scope.public.preview.grid": "Active Grid",
    "scope.public.rollout.cells": "public rollout cells",
    "scope.public.rollout.preview": "Active Grid",
    "scope.rollout.totals": "Rollout totals",
    "scope.rollout.status": "Rollout status",
    "scope.scale": "Scale",
    "scope.v9.pilot.status": "V9 pilot status",
    "scope.v9.pilot.totals": "V9 pilot totals",
    "scope.workspace": "Workspace",
    "workflow.monitorForestAssets": "Monitor forest assets",
    "workflow.verifyAnomalies": "Verify anomalies",
    "workflow.prepareCarbonModel": "Prepare carbon model",
    zoomIn: "Zoom in",
    zoomOut: "Zoom out",
    "event.auditEvent": "Audit event",
    "event.created": "Created",
    "event.statusChanged": "Status changed",
    "event.statusRecalculated": "Recalculated",
    "observation.noNdvi": "No NDVI",
    "observation.noValidPixels": "No valid pixels",
    "observation.notObserved": "Awaiting observation",
    "observation.ok": "Valid Sentinel NDVI",
    "observation.requestFailed": "Request failed",
    "observation.unknown": "Unknown Sentinel status",
    "quality.publicForestMaskPreview": "Public forest mask",
    "quality.publicOgmRest": "Public OGM REST",
    "quality.publicWmsDerived": "Public WMS-derived",
    "relation.currentLayerA": "Intersects current Layer A",
    "relation.noLayerA": "No current Layer A match",
    "relation.none": "No relation",
    "sourceAge.days": "{{days}}d old",
    "sourceCheck.latestOk": "Latest check OK",
    "sourceCheck.latestUnavailable": "Latest check unavailable",
    "sourceCheck.none": "No source check",
    "sourceCheck.pending": "Check pending",
    "sourceCheck.retained": "Last data retained",
    "sourceCheck.unavailable": "Source check unavailable",
    "sourceFreshness.ageDays": "data {{days}}d old",
    "sourceFreshness.noRetainedData": "No retained data",
    "sourceFreshness.previewRetained": "forest mask retained",
    "sourceQuality.degraded": "Some public data sources are degraded.",
    "status.anomaly": "Anomaly",
    "status.insufficientData": "Insufficient data",
    "status.noResult": "No result",
    "status.watch": "Watch",
  },
};

export function availableLocales(catalog = messages) {
  return uniqueLocales(Object.keys(catalog).filter((locale) => Boolean(catalog[locale])));
}

export function normalizeLocale(value) {
  const normalized = String(value || "").trim().toLowerCase().replaceAll("_", "-");
  if (!normalized) {
    return "";
  }
  const locale = localeAliases[normalized] || normalized;
  if (locale.length > MAX_LOCALE_LENGTH) {
    return "";
  }
  return LOCALE_PATTERN.test(locale) ? locale : "";
}

export function parseLocaleList(value) {
  if (Array.isArray(value)) {
    return uniqueLocales(value);
  }
  return uniqueLocales(String(value || "").split(","));
}

export function resolveLocale(requested, allowedLocales = availableLocales()) {
  const allowed = uniqueLocales(allowedLocales);
  const fallback = allowed.includes(FALLBACK_LOCALE) ? FALLBACK_LOCALE : allowed[0] || DEFAULT_LOCALE;
  const normalized = normalizeLocale(requested);
  if (!normalized) {
    return fallback;
  }
  if (allowed.includes(normalized)) {
    return normalized;
  }
  const language = normalized.split("-")[0];
  return allowed.includes(language) ? language : fallback;
}

export function createI18nResources(catalog = messages) {
  return Object.fromEntries(
    Object.entries(catalog)
      .map(([locale, translation]) => [normalizeLocale(locale), translation])
      .filter(([locale, translation]) => locale && translation)
      .map(([locale, translation]) => [locale, { translation }])
  );
}

export function mergeLanguagePack(catalog, locale, translation) {
  const normalized = normalizeLocale(locale);
  if (!normalized || !translation || typeof translation !== "object") {
    return catalog;
  }
  return {
    ...catalog,
    [normalized]: {
      ...(catalog[normalized] || {}),
      ...translation,
    },
  };
}

function uniqueLocales(locales) {
  const result = [];
  locales.forEach((locale) => {
    const normalized = normalizeLocale(locale);
    if (normalized && !result.includes(normalized)) {
      result.push(normalized);
    }
  });
  return result;
}
