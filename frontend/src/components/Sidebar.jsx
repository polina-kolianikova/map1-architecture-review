import { Activity, Database, Layers, Leaf, ShieldCheck } from "lucide-react";

import { EmptyLine, Metric, PanelHeader } from "./common";
import { useI18n } from "../hooks/useI18n";
import { formatInteger, formatNumber, formatPercent, shortId } from "../lib/formatters";
import {
  eventLabel,
  observationLabel,
  qualityLabel,
  sourceCheckLabel,
  sourceFreshnessLabel,
  statusLabel,
} from "../lib/status";

export function Sidebar({
  auditEmptyText,
  displaySummary,
  loading,
  observationStatusCounts,
  onChooseStatusFilter,
  selectedAuditEvents,
  scope,
  statusCounts,
  summary,
  verificationStatusFilter,
}) {
  const { t } = useI18n();
  const layerBStatus = displaySummary.stage4_layer_b;
  const carbonSummary = displaySummary.stage5_carbon;
  const evidenceLogLabel = layerBStatus ? t("layerBBatches") : t("evidenceLog");
  const evidenceLogValue = layerBStatus
    ? layerBStatus.total_batches
    : displaySummary.verification_audit_events;

  return (
    <aside className="sidebar">
      <section className="panel panel-summary">
        <PanelHeader
          icon={Database}
          title={t(scopeTextKey(scope?.sidebarTitle), { defaultValue: scope?.sidebarTitle || t("workspaceTotals") })}
        />
        {summary?.summary_updated_at && (
          <p className="summary-timestamp">{t("summaryAsOf", { time: new Date(summary.summary_updated_at * 1000).toLocaleString() })}</p>
        )}
        <div className="metric-grid">
          <Metric label={t("assets")} value={formatInteger(displaySummary.grid_cells)} loading={loading && !summary} />
          <Metric label={t("registry")} value={formatInteger(displaySummary.ogm_parcels)} loading={loading && !summary} />
          <Metric
            label={t("ndviCells")}
            value={formatInteger(displaySummary.real_sentinel_observations)}
            loading={loading && !summary}
          />
          <Metric label={t("signalCover")} value={formatPercent(displaySummary.ndvi_coverage_ratio)} loading={loading && !summary} />
          <Metric
            label={t("statusRows")}
            value={formatInteger(displaySummary.verification_results)}
            loading={loading && !summary}
          />
          <Metric
            label={evidenceLogLabel}
            value={formatInteger(evidenceLogValue)}
            loading={loading && !summary}
          />
        </div>
      </section>

      {carbonSummary && (
        <section className="panel panel-carbon">
          <PanelHeader icon={Leaf} title={t("carbonReadinessTitle")} />
          <div className="metric-grid">
            <Metric
              label={t("totalPotential")}
              value={formatCarbonTotal(carbonSummary.total_potential_tco2e, "tCO2e")}
              loading={loading && !summary}
            />
            <Metric
              label={t("netAnnualIncrement")}
              value={formatCarbonTotal(carbonSummary.net_annual_increment_tco2e_yr, "tCO2e/year")}
              loading={loading && !summary}
            />
            <Metric
              label={t("meanAnnualRate")}
              value={formatCarbonTotal(carbonSummary.average_net_annual_increment_tco2e_ha_yr, "tCO2e/ha/year")}
              loading={loading && !summary}
            />
            <Metric
              label={t("confidence")}
              value={formatCarbonPercent(carbonSummary.average_confidence_score)}
              loading={loading && !summary}
            />
            {carbonSummary.recalculation_required && (
              <Metric
                label={t("calculationState")}
                value={statusText(carbonSummary.summary_state, t)}
                loading={loading && !summary}
              />
            )}
          </div>
        </section>
      )}

      <section className="panel panel-sources">
        <PanelHeader icon={Layers} title={t("registrySources")} />
        <div className="source-list">
          {(displaySummary.layer_a_sources || []).map((source) => (
            <div className="source-row" key={`${source.source_name}-${source.official_status}`}>
              <div>
                <strong>{source.source_name || t("layerASource")}</strong>
                <span>
                  {qualityLabel(source.source_quality || source.official_status, t)} /{" "}
                  {sourceCheckLabel(source, t)} / {sourceFreshnessLabel(source, t)}
                </span>
              </div>
              <b>{formatInteger(source.feature_count)}</b>
            </div>
          ))}
          {!displaySummary.layer_a_sources?.length && (
            <EmptyLine text={loading ? t("loadingData") : t("noLayerARecords")} />
          )}
        </div>
      </section>

      <section className="panel panel-observations">
        <PanelHeader icon={Activity} title={t("satelliteSignal")} />
        <div className="observation-list">
          {observationStatusCounts.map((item) => (
            <div className="observation-row" key={item.status}>
              <span className={`status-dot status-${item.status}`} />
              <span>{observationLabel(item.status, t)}</span>
              <b>{item.count}</b>
            </div>
          ))}
          {!observationStatusCounts.length && (
            <EmptyLine text={loading ? t("loadingData") : t("noSentinelObservations")} />
          )}
        </div>
      </section>

      <section className="panel panel-status">
        <PanelHeader icon={Activity} title={t("assetStatus")} />
        <div className="status-list">
          {statusCounts.map((item) => (
            <button
              aria-pressed={verificationStatusFilter === item.anomaly_type}
              className={`status-row status-filter-row ${
                verificationStatusFilter === item.anomaly_type ? "is-active" : ""
              }`}
              key={item.anomaly_type}
              onClick={() => onChooseStatusFilter(item.anomaly_type)}
              type="button"
            >
              <span className={`status-dot status-${item.anomaly_type}`} />
              <span>{statusLabel(item.anomaly_type, t)}</span>
              <b>{formatInteger(item.result_count)}</b>
            </button>
          ))}
          {!statusCounts.length && (
            <EmptyLine text={loading ? t("loadingData") : t("noVerificationResultsYet")} />
          )}
        </div>
      </section>

      <section className="panel panel-audit">
        <PanelHeader icon={ShieldCheck} title={t("evidenceLog")} />
        <div className="audit-list">
          {selectedAuditEvents.slice(0, 6).map((event) => (
            <div className="audit-row" key={event.event_id}>
              <span className={`status-dot status-${event.new_status}`} />
              <div>
                <strong>{eventLabel(event.event_type, t)}</strong>
                <span>
                  {shortId(event.cell_uuid)} - {statusLabel(event.previous_status, t)} {t("auditTransitionTo")}{" "}
                  {statusLabel(event.new_status, t)}
                </span>
              </div>
            </div>
          ))}
          {!selectedAuditEvents.length && <EmptyLine text={auditEmptyText} />}
        </div>
      </section>
    </aside>
  );
}

function scopeTextKey(value) {
  return `scope.${String(value || "").trim().replaceAll(/\s+/g, ".").toLowerCase()}`;
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

function formatCarbonPercent(value) {
  if (value === null || value === undefined || (typeof value === "string" && value.trim() === "")) {
    return "n/a";
  }
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "n/a";
  }
  return `${formatNumber(number)}%`;
}

function statusText(value, t) {
  if (!value) {
    return t("current");
  }
  return String(value).replaceAll("_", " ");
}
