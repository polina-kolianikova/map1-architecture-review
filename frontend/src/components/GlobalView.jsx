import { useEffect, useRef, useState } from "react";
import { ArrowRight, Globe2 } from "lucide-react";

import { MAPBOX_TOKEN } from "../config/runtime";
import { formatInteger, formatNumber, formatPercent } from "../lib/formatters";
import { useI18n } from "../hooks/useI18n";
import { statusLabel } from "../lib/status";

const workflowSteps = [
  ["workflow.monitorForestAssets", "Monitor forest assets"],
  ["workflow.verifyAnomalies", "Verify anomalies"],
  ["workflow.prepareCarbonModel", "Prepare carbon model"],
];

export function GlobalView({
  cellCount,
  carbonSummary,
  dataQuality,
  layerCount,
  loading,
  ndviCoverageRatio,
  dataReady = false,
  loadError = false,
  summaryUpdatedAt,
  regions = [],
  resultCount,
  scope,
  scopes = [],
  statusCounts = [],
  onEnter,
  onScopeChange,
  onSelectStatus,
}) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const { t } = useI18n();
  const [mapFailed, setMapFailed] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!MAPBOX_TOKEN || !containerRef.current || mapRef.current) {
      return undefined;
    }

    let cancelled = false;
    import("mapbox-gl").then(({ default: mapboxgl }) => {
      if (cancelled || !containerRef.current) {
        return;
      }
      mapboxgl.accessToken = MAPBOX_TOKEN;
      const map = new mapboxgl.Map({
        container: containerRef.current,
        style: "mapbox://styles/mapbox/dark-v11",
        center: [35.2, 39],
        zoom: 1.45,
        projection: "globe",
        interactive: true,
        attributionControl: true,
      });
      map.scrollZoom.disable();
      map.dragRotate.enable();
      map.touchZoomRotate.enable();
      map.on("style.load", () => {
        map.setFog({
          color: "rgb(10, 16, 22)",
          "high-color": "rgb(28, 52, 68)",
          "horizon-blend": 0.18,
          "space-color": "rgb(4, 8, 12)",
          "star-intensity": 0.25,
        });
      });
      map.on("load", () => {
        setMapFailed(false);
        setReady(true);
        map.easeTo({
          center: [35.2, 39],
          zoom: 2.25,
          duration: 1800,
          easing: (time) => time * (2 - time),
        });
        map.rotateTo(-10, {
          duration: 1800,
          easing: (time) => time * (2 - time),
        });
      });
      mapRef.current = map;
    }).catch(() => {
      setMapFailed(true);
      setReady(false);
    });

    return () => {
      cancelled = true;
      setReady(false);
      mapRef.current?.remove();
      mapRef.current = null;
    };
  }, []);

  return (
    <section className="global-view" aria-label={t("portfolioOverview")}>
      <div className={`global-map ${ready ? "is-ready" : ""}`} ref={containerRef}>
      </div>
      {(!MAPBOX_TOKEN || mapFailed || !ready) && (
        <div className="global-fallback-globe" aria-hidden="true" />
      )}
      <div className="global-vignette" />
      <div className="global-content">
        <div className="global-copy">
          <p className="eyebrow">{t("forestAssetIntelligence")}</p>
          <h1>Map 1 Turkey</h1>
          <p className="global-lede">{t("auditBackedEvidence")}</p>
          <p className="global-scope-note">
            {t(scopeTextKey(scope?.globalNote), { defaultValue: scope?.globalNote })}
          </p>
          <div className="global-scope-switcher" aria-label={t("workspaceScope")}>
            {scopes.map((item) => (
              <button
                aria-pressed={scope?.key === item.key}
                className={scope?.key === item.key ? "is-active" : ""}
                key={item.key}
                disabled={item.available === false}
                title={item.available === false ? t("pilotScopeUnavailable") : undefined}
                onClick={() => onScopeChange?.(item.key)}
                type="button"
              >
                {t(scopeTextKey(item.label), { defaultValue: item.label })}
              </button>
            ))}
          </div>
          <div className="global-command-meta" aria-label={t("workspaceOperatingState")}>
            {(scope?.globalMeta || []).map(([label, value]) => (
              <span key={label}>
                <small>{t(scopeTextKey(label), { defaultValue: label })}</small>
                <b>{t(scopeTextKey(value), { defaultValue: value })}</b>
              </span>
            ))}
          </div>
          <div className="global-workflow" aria-label={t("portfolioWorkflow")}>
            {workflowSteps.map(([key, step], index) => (
              <span key={key}>
                <b>{String(index + 1).padStart(2, "0")}</b>
                {t(key, { defaultValue: step })}
              </span>
            ))}
          </div>
          <div className="global-actions">
            <button className="primary-action" type="button" onClick={onEnter} disabled={!dataReady}>
              <Globe2 size={18} />
              <span>{dataReady ? t("openTurkeyWorkspace") : t(loadError ? "dataUnavailable" : "loadingData")}</span>
              <ArrowRight size={18} />
            </button>
          </div>
        </div>
        <div className="global-side">
          <h2 className="global-section-title">{t("carbonReadinessTitle")}</h2>
          {summaryUpdatedAt && (
            <p className="summary-timestamp">{t("summaryAsOf", { time: new Date(summaryUpdatedAt * 1000).toLocaleString() })}</p>
          )}
          <div className="global-stats" aria-label={t("currentPortfolioSnapshot")}>
            <GlobalStat label={t("gridCells")} loading={loading} t={t} value={dataReady ? formatInteger(cellCount) : "n/a"} />
            <GlobalStat label={t("layerARecords")} loading={loading} t={t} value={dataReady ? formatInteger(layerCount) : "n/a"} />
            <GlobalStat label={t("verificationResults")} loading={loading} t={t} value={dataReady ? formatInteger(resultCount) : "n/a"} />
            <GlobalStat label={t("ndviCoverage")} loading={loading} t={t} value={dataReady ? formatPercent(ndviCoverageRatio) : "n/a"} />
            {carbonSummary && (
              <GlobalStat
                label={t("totalPotential")}
                loading={loading}
                t={t}
                value={formatCarbonTotal(carbonSummary.total_potential_tco2e, "tCO2e")}
              />
            )}
            {carbonSummary && (
              <GlobalStat
                label={t("netAnnualIncrement")}
                loading={loading}
                t={t}
                value={formatCarbonTotal(carbonSummary.net_annual_increment_tco2e_yr, "tCO2e/year")}
              />
            )}
            <GlobalStat label={t("dataQualityLabel")} loading={loading} t={t} value={qualityLabel(dataQuality, t)} />
            <GlobalStat
              label={scope?.isPreview ? t("mode") : t("expansionTarget")}
              tone="target"
              value={scope?.isPreview ? t("preview") : "23M ha"}
            />
          </div>
          <section
            className="global-risk"
            aria-label={`${t(scopeTextKey(scope?.signalTitle), { defaultValue: scope?.signalTitle || t("workspaceSignal") })} distribution`}
          >
            <span>{t(scopeTextKey(scope?.signalTitle), { defaultValue: scope?.signalTitle || t("workspaceSignal") })}</span>
            <div>
              {statusCounts.map((item) => (
                <button
                  className={`portfolio-signal-row status-${item.anomaly_type}`}
                  key={item.anomaly_type}
                  disabled={!dataReady}
                  onClick={() => onSelectStatus?.(item.anomaly_type)}
                  type="button"
                >
                  <strong>{statusLabel(item.anomaly_type, t)}</strong>
                  <b>{dataReady ? formatInteger(item.result_count) : "n/a"}</b>
                </button>
              ))}
            </div>
          </section>
          <section className="global-portfolio" aria-label={scope?.isPreview ? t("fullPreviewWorkspace") : t("portfolioRegions")}>
            <span>{scope?.isPreview ? t("fullPreviewWorkspace") : t("portfolioRegions")}</span>
            <div>
              {regions.map((region) => (
                <button
                  aria-label={t("openWorkspace", {
                    name: region.countryCode === "TR" ? t("turkey") : region.name || region.code,
                  })}
                  className="portfolio-region-row"
                  key={region.id || region.code}
                  disabled={!dataReady}
                  onClick={onEnter}
                  type="button"
                >
                  <strong>{region.countryCode === "TR" ? t("turkey") : region.countryCode}</strong>
                  <em>{regionLabel(region, scope, t)}</em>
                  <b>{t(scopeTextKey(scope?.regionStatus), { defaultValue: scope?.regionStatus || t("regionStatusActive") })}</b>
                </button>
              ))}
              {!regions.length && (
                <button
                  aria-label={t("openWorkspace", { name: t("turkey") })}
                  className="portfolio-region-row"
                  disabled={!dataReady}
                  onClick={onEnter}
                  type="button"
                >
                  <strong>{t("turkey")}</strong>
                  <em>{dataReady ? regionLabel({}, scope, t) : t(loadError ? "dataUnavailable" : "loadingData")}</em>
                  <b>{dataReady ? t(scopeTextKey(scope?.regionStatus), { defaultValue: scope?.regionStatus || t("regionStatusActive") }) : t(loadError ? "dataUnavailable" : "loadingData")}</b>
                </button>
              )}
            </div>
          </section>
        </div>
      </div>
    </section>
  );
}

function GlobalStat({ label, loading = false, tone = "", value, t = (key) => key }) {
  return (
    <div className={tone ? `is-${tone}` : ""}>
      <span>{label}</span>
      <strong>{loading ? t("loading") : value}</strong>
    </div>
  );
}

function qualityLabel(dataQuality, t) {
  if (!dataQuality?.status) {
    return t("pending");
  }
  return dataQuality.status === "ok" ? t("qualityOk") : t("degraded");
}

function formatCarbonTotal(value, unit) {
  if (value === null || value === undefined || (typeof value === "string" && value.trim() === "")) {
    return "n/a";
  }
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "n/a";
  }
  const abs = Math.abs(number);
  if (abs >= 1_000_000_000) {
    return `${formatNumber(number / 1_000_000_000)}B ${unit}`;
  }
  if (abs >= 1_000_000) {
    return `${formatNumber(number / 1_000_000)}M ${unit}`;
  }
  if (abs >= 1_000) {
    return `${formatNumber(number / 1_000)}K ${unit}`;
  }
  return `${formatNumber(number)} ${unit}`;
}

function regionLabel(region, scope, t) {
  if (scope?.isPreview) {
    return t("fullPublicForestRolloutPreview");
  }
  if (region.countryCode === "TR") {
    return t("turkeyPilotWorkspace");
  }
  return region.name || region.code || t("portfolioWorkspace");
}

function scopeTextKey(value) {
  return `scope.${String(value || "").trim().replaceAll(/\s+/g, ".").toLowerCase()}`;
}
