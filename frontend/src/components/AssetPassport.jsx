import { ShieldCheck, X } from "lucide-react";

import {
  cellLabel,
  formatDate,
  formatInteger,
  formatNumber,
  formatPercent,
  formatPercentFromWhole,
} from "../lib/formatters";
import {
  observationLabel,
  qualityLabel,
  relationLabel,
  sourceAgeLabel,
  sourceCheckStatusLabel,
  statusLabel,
} from "../lib/status";
import { useI18n } from "../hooks/useI18n";
import { getNdviTrend } from "../lib/ndviHistory";
import { getAssetAudit } from "../lib/assetAudit";

export function AssetPassport({
  ndviHistory = [],
  ndviHistoryError = "",
  ndviHistoryLoading = false,
  ndviHistoryMonths = 12,
  onClearSelection,
  onNdviHistoryMonthsChange,
  row,
}) {
  const { t } = useI18n();
  if (!row) {
    return null;
  }
  const carbon = row.carbon_readiness || {};
  const annual = carbon.annual_increment || {};
  const yoy = carbon.year_over_year || {};
  const carbonNotice = carbonMethodologyNotice(carbon, row, t);
  const audit = getAssetAudit(row);
  const auditCountLabel = (count) => count === null ? t("dataUnavailable") : t("carbonAuditEvents", {
    count,
    formattedCount: formatInteger(count),
  });
  const auditBreakdown = t("assetAuditBreakdown", {
    verification: audit.verificationCount === null ? t("dataUnavailable") : formatInteger(audit.verificationCount),
    carbon: audit.carbonCount === null ? t("dataUnavailable") : formatInteger(audit.carbonCount),
  });

  return (
    <section className="asset-passport" aria-label={t("assetPassport")}>
      <header className="asset-passport-header">
        <div>
          <span>{t("assetPassport")}</span>
          <strong>{cellLabel(row.cell_uuid)}</strong>
        </div>
        <div className="asset-passport-actions">
          <b className={`passport-status status-${row.anomaly_type}`}>{statusLabel(row.anomaly_type, t)}</b>
          {onClearSelection && (
            <button aria-label={t("clearAssetPassport")} onClick={onClearSelection} type="button">
              <X size={14} />
              <span>{t("clear")}</span>
            </button>
          )}
        </div>
      </header>

      <div className="passport-hero" aria-label={t("selectedAssetSummary")}>
        <div>
          <span>{t("evidence")}</span>
          <strong>{statusLabel(row.anomaly_type, t)}</strong>
          <small>{t("verificationRiskScore", { score: formatNumber(row.score) })}</small>
          <small>{t("verificationRiskScoreHint")}</small>
        </div>
        <div>
          <span>{t("satelliteSignal")}</span>
          <strong>NDVI {formatNumber(row.ndvi)}</strong>
          <small>{formatDate(row.acquisition_date)}</small>
        </div>
        <div>
          <span>{t("registryMatch")}</span>
          <strong>{qualityLabel(row.layer_a_source_quality, t) || t("pending")}</strong>
          <small>{sourceAgeLabel(row.layer_a_data_age_days, t)}</small>
        </div>
      </div>

      <section className="co2-foundation" aria-label={t("carbonModelInputs")}>
        <div className="co2-heading">
          <span>{t("carbonReadiness")}</span>
          <strong>{carbonReadinessTitle(carbon, row, t)}</strong>
        </div>
        <div className="co2-grid">
          <Co2Slot
            label={t("totalPotential")}
            value={carbonValue(row.carbon_total_potential_tco2e, carbon.total_potential, t)}
            status={carbonLabel(carbon.total_potential?.status || row.carbon_total_potential_status, t("awaitingCoefficients"), t)}
            featured
          />
          <Co2Slot
            label={t("netAnnualIncrement")}
            value={carbonAnnualValue(
              row.carbon_net_annual_increment_tco2e_ha_yr,
              annual.net_per_ha_year,
              "tCO2e/ha/year",
              t
            )}
            status={carbonLabel(annual.net_per_ha_year?.status || row.carbon_calculation_status, t("awaitingEvidence"), t)}
            featured
          />
          <Co2Slot
            label={t("soilCarbon")}
            value={carbonValue(row.carbon_soil_tco2e, carbon.soil, t)}
            status={carbonLabel(carbon.soil?.input_status || row.carbon_soil_input_status, t("inputSlotReady"), t)}
          />
          <Co2Slot
            label={t("biomassCarbon")}
            value={carbonValue(row.carbon_biomass_tco2e, carbon.biomass, t)}
            status={carbonLabel(carbon.biomass?.input_status || row.carbon_biomass_input_status, t("inputSlotReady"), t)}
          />
          <Co2Slot
            label={t("rootSystem")}
            value={carbonValue(row.carbon_root_tco2e, carbon.root_system, t)}
            status={carbonLabel(carbon.root_system?.input_status || row.carbon_root_input_status, t("inputSlotReady"), t)}
          />
          <Co2Slot
            label={t("coefficientSet")}
            value={displayValue(
              carbon.coefficient_set_version ||
                row.carbon_coefficient_set_version ||
                carbonLabel(carbon.coefficient_status || row.carbon_coefficient_status, "", t),
              t("coefficientPending")
            )}
          />
          <Co2Slot
            label={t("methodologyVersion")}
            value={displayValue(carbon.methodology_version || row.carbon_methodology_version, t("methodologyPending"))}
          />
          <Co2Slot
            label={t("verificationFactor")}
            value={carbonNumber(row.carbon_verification_factor ?? carbon.verification_factor, t)}
            status={carbonLabel(carbon.verification_factor_status, t("awaitingCoefficients"), t)}
          />
          <Co2Slot
            label={t("grossIncrement")}
            value={carbonAnnualValue(
              row.carbon_gross_increment_tco2e_ha_yr,
              annual.gross_increment,
              "tCO2e/ha/year",
              t
            )}
            status={carbonLabel(annual.gross_increment?.source, t("baselineConfig"), t)}
          />
          <Co2Slot
            label={t("lossFactor")}
            value={carbonAnnualValue(row.carbon_loss_factor_tco2e_ha_yr, annual.loss_factor, "tCO2e/ha/year", t)}
            status={carbonLabel(annual.loss_factor?.source, t("riskModel"), t)}
          />
          <Co2Slot
            label={t("annualAssetRate")}
            value={carbonAnnualValue(
              row.carbon_net_annual_increment_tco2e_yr,
              annual.net_per_asset_year,
              "tCO2e/year",
              t
            )}
            status={carbonLabel(annual.increment_model || row.carbon_increment_model, t("incrementModel"), t)}
          />
          <Co2Slot
            label={t("yoyComparison")}
            value={carbonLabel(yoy.comparison_state, t("awaitingAnnualSlices"), t)}
            status={yoy.previous_year && yoy.current_year ? `${yoy.previous_year} to ${yoy.current_year}` : ""}
          />
          <Co2Slot
            label={t("annualDeltaHa")}
            value={carbonAnnualValue(
              row.carbon_net_annual_increment_delta_tco2e_ha_yr,
              annual.net_delta_per_ha_year,
              "tCO2e/ha/year",
              t
            )}
            status={carbonLabel(annual.net_delta_per_ha_year?.status || yoy.comparison_state, t("awaitingAnnualSlices"), t)}
          />
          <Co2Slot
            label={t("annualAssetDelta")}
            value={carbonAnnualValue(
              row.carbon_net_annual_increment_delta_tco2e_yr,
              annual.net_delta_per_asset_year,
              "tCO2e/year",
              t
            )}
            status={carbonLabel(annual.net_delta_per_asset_year?.status || yoy.comparison_state, t("awaitingAnnualSlices"), t)}
          />
          <Co2Slot
            label={t("carbonConfidence")}
            value={carbonPercent(row.carbon_confidence_score ?? carbon.confidence_score, t)}
            status={t("carbonConfidenceHint")}
          />
          <Co2Slot
            label={t("co2Signal")}
            value={carbonAnomalyLabel(carbon.anomaly_type, t)}
            status={carbonLabel(carbon.evidence_state, t("awaitingEvidence"), t)}
          />
          <Co2Slot
            label={t("calibrationSource")}
            value={carbonLabel(carbon.calibration_source || row.carbon_calibration_source, t("reserved"), t)}
            status={carbonLabel(carbon.layer_d_status, t("layerDReserved"), t)}
          />
          <Co2Slot label={t("evidenceState")} value={carbonLabel(carbon.evidence_state, t("awaitingEvidence"), t)} />
          <Co2Slot label={t("methodologyState")} value={carbonLabel(carbon.methodology_state, t("coefficientPending"), t)} />
          <Co2Slot
            label={t("coefficientSource")}
            value={coefficientSourceLabel(
              carbon.coefficient_scope ||
                row.carbon_coefficient_scope ||
                carbon.coefficient_status ||
                row.carbon_coefficient_status ||
                row.carbon_calculation_status,
              t
            )}
          />
          <Co2Slot
            label={t("assetAudit")}
            value={auditCountLabel(audit.totalCount)}
            status={auditBreakdown}
          />
        </div>
        {carbonNotice && <p className="co2-methodology-note">{carbonNotice}</p>}
      </section>

      <details className="passport-section" open>
        <summary>
          {t("assetIdentity")}
          <b>{row.area_ha ? `${formatNumber(row.area_ha)} ha` : t("dataUnavailable")}</b>
        </summary>
        <div className="passport-grid">
          <PassportMetric label={t("uuid")} value={row.cell_uuid} wide />
          <PassportMetric label={t("area")} value={row.area_ha ? `${formatNumber(row.area_ha)} ha` : t("dataUnavailable")} />
          <PassportMetric label={t("areaCoefficient")} value={formatNumber(row.area_coefficient)} />
          <PassportMetric label={t("region")} value={regionLabel(row.region_code, t)} />
        </div>
      </details>

      <details className="passport-section" open>
        <summary>
          {t("satelliteEvidence")}
          <b>NDVI {formatNumber(row.ndvi)}</b>
        </summary>
        <NdviTrend
          history={ndviHistory}
          loading={ndviHistoryLoading}
          error={ndviHistoryError}
          months={ndviHistoryMonths}
          onMonthsChange={onNdviHistoryMonthsChange}
          t={t}
        />
        <div className="passport-grid">
          <PassportMetric label={t("acquisition")} value={formatDate(row.acquisition_date)} />
          <PassportMetric label={t("previousNdvi")} value={formatNumber(row.previous_ndvi)} />
          <PassportMetric label={t("previousAcquisition")} value={formatDate(row.previous_acquisition_date)} />
          <PassportMetric label={t("lastObservation")} value={observationLabel(row.observation_status, t)} />
          <PassportMetric
            label={t("validPixels")}
            value={`${formatPercent(row.observation_valid_pixel_ratio)}${
              row.observation_valid_pixel_count !== null &&
              row.observation_valid_pixel_count !== undefined
                ? ` (${formatInteger(row.observation_valid_pixel_count)} px)`
                : ""
            }`}
          />
          <PassportMetric label={t("sampleCount")} value={formatInteger(row.observation_sample_count)} />
          <PassportMetric label={t("cloud")} value={formatPercentFromWhole(row.observation_cloud_coverage)} />
        </div>
      </details>

      <details className="passport-section">
        <summary>
          {t("registryEvidence")}
          <b>{qualityLabel(row.layer_a_source_quality, t) || t("noMatch")}</b>
        </summary>
        <div className="passport-grid">
          <PassportMetric label={t("layerA")} value={row.layer_a_source_name || t("noIntersectingSource")} />
          <PassportMetric label={t("sourceQuality")} value={qualityLabel(row.layer_a_source_quality, t) || t("noLayerAMatch")} />
          <PassportMetric label={t("sourceCheck")} value={sourceCheckStatusLabel(row.layer_a_latest_check_status, t)} />
          <PassportMetric label={t("dataAge")} value={sourceAgeLabel(row.layer_a_data_age_days, t)} />
          <PassportMetric label={t("coverage")} value={formatPercent(row.layer_a_coverage_ratio)} />
          <PassportMetric label={t("relation")} value={relationLabel(row.layer_a_relation, t)} />
        </div>
      </details>

      <details className="passport-section">
        <summary>
          {t("auditEvidence")}
          <b>{auditCountLabel(audit.totalCount)}</b>
        </summary>
        <div className="passport-grid">
          <PassportMetric label={t("verificationAudit")} value={auditCountLabel(audit.verificationCount)} />
          <PassportMetric label={t("lastVerificationEvent")} value={formatDate(audit.latestVerificationEventAt)} />
          <PassportMetric label={t("carbonAudit")} value={auditCountLabel(audit.carbonCount)} />
          <PassportMetric label={t("lastCarbonEvent")} value={formatDate(audit.latestCarbonEventAt)} />
        </div>
      </details>

      <div className="passport-note">
        <ShieldCheck size={16} />
        <p>{row.explanation || t("noExplanationRecorded")}</p>
      </div>
    </section>
  );
}

function PassportMetric({ detail = false, label, value, wide = false }) {
  const displayValue = value === null || value === undefined || value === "" ? "n/a" : value;
  return (
    <div className={`${wide ? "is-wide" : ""} ${detail ? "is-detail" : ""}`.trim()}>
      <span>{label}</span>
      {detail ? (
        <p title={String(displayValue)}>{displayValue}</p>
      ) : (
        <strong title={String(displayValue)}>{displayValue}</strong>
      )}
    </div>
  );
}

function Co2Slot({ featured = false, label, value, status = "" }) {
  return (
    <div className={featured ? "is-featured" : ""}>
      <span>{label}</span>
      <strong>{value}</strong>
      {status && <small>{status}</small>}
    </div>
  );
}

function NdviTrend({ history, loading, error, months = 12, onMonthsChange, t }) {
  const { points, delta, messageKey } = getNdviTrend(history);
  const values = points.map((item) => item.ndvi);
  const latest = points.at(-1);
  const first = points.at(0);

  return (
    <div className="ndvi-trend">
      <div className="ndvi-trend-header">
        <span>{t("ndviTrend")}</span>
        <strong>{trendLabel(delta, loading, error, points.length, t)}</strong>
      </div>
      <div className="ndvi-range-toggle" aria-label={t("ndviHistoryRange")}>
        {[6, 12].map((value) => (
          <button
            className={months === value ? "is-active" : ""}
            key={value}
            onClick={() => onMonthsChange?.(value)}
            type="button"
          >
            {value}M
          </button>
        ))}
      </div>
      {points.length >= 2 ? (
        <svg className="ndvi-sparkline" viewBox="0 0 240 70" role="img" aria-label={t("ndviTrend")}>
          <polyline className="ndvi-grid-line" points="0,58 240,58" />
          <polyline className="ndvi-grid-line" points="0,35 240,35" />
          <polyline className="ndvi-grid-line" points="0,12 240,12" />
          <polyline className="ndvi-line" points={sparklinePoints(values)} />
          {points.map((item, index) => (
            <circle
              className="ndvi-point"
              cx={sparklineX(index, points.length)}
              cy={sparklineY(item.ndvi)}
              key={`${item.date}-${index}`}
              r="2.8"
            />
          ))}
        </svg>
      ) : (
        <p className="ndvi-trend-empty">
          {loading ? t("loadingNdviHistory") : error || (messageKey ? t(messageKey) : ndviHistoryEmptyText(history, t))}
        </p>
      )}
      <div className="ndvi-trend-meta">
        <span>{first ? formatDate(first.date) : t("none")}</span>
        <span>{latest ? formatDate(latest.date) : t("none")}</span>
      </div>
    </div>
  );
}

function carbonLabel(value, fallback, t) {
  if (!value) {
    return fallback;
  }
  const normalized = String(value).replaceAll("_", " ").toLowerCase();
  const labels = {
    "model ready": ["frameworkPrepared", "Framework prepared"],
    "future analytics module": ["frameworkPrepared", "Framework prepared"],
    "pending coefficients": ["awaitingCoefficients", "Awaiting coefficients"],
    "awaiting coefficients": ["awaitingCoefficients", "Awaiting coefficients"],
    "pending approval": ["coefficientPending", "Coefficient pending"],
    "baseline": ["baselineDraft", "Baseline Assessment"],
    "baseline estimate": ["baselineEstimate", "Baseline estimate"],
    "draft": ["draft", "Baseline Assessment"],
    "draft estimate": ["draftEstimate", "Baseline Assessment"],
    "approved coefficients loaded": ["approvedCoefficientsLoaded", "Approved coefficients loaded"],
    "configured without coefficients": ["configured", "Configured"],
    "calculated": ["calculated", "Calculated"],
    "pending": ["awaitingInputs", "Awaiting inputs"],
    "pending inputs": ["awaitingInputs", "Awaiting inputs"],
    "skipped missing evidence": ["missingEvidence", "Missing evidence"],
    "missing verification or ndvi": ["missingEvidence", "Missing evidence"],
    "awaiting verification": ["awaitingVerification", "Awaiting verification"],
    "awaiting satellite signal": ["awaitingSatelliteSignal", "Awaiting satellite signal"],
    "awaiting annual slices": ["awaitingAnnualSlices", "Awaiting annual slices"],
    "awaiting current year": ["awaitingCurrentYear", "Awaiting current year"],
    "awaiting previous year": ["awaitingPreviousYear", "Awaiting previous year"],
    "awaiting previous carbon snapshot": ["awaitingPreviousCarbonSnapshot", "Awaiting previous carbon snapshot"],
    "annual baseline": ["baselineConfig", "Annual baseline"],
    "ready": ["regionStatusReady", "Ready"],
    "failed": ["failed", "Failed"],
    "reserved": ["awaitingCoefficients", "Awaiting coefficients"],
    "reserved for analytics": ["awaitingCoefficients", "Awaiting coefficients"],
    "required": ["inputSlotReady", "Input slot ready"],
    "not approved": ["coefficientPending", "Coefficient pending"],
    "not assigned": ["methodologyPending", "Model pending"],
  };
  const match = labels[normalized];
  return match ? translateLabel(t, match[0], match[1]) : normalized;
}

function carbonReadinessTitle(carbon, row, t) {
  const coefficientStatus = carbon.coefficient_status || row.carbon_coefficient_status;
  const status =
    carbon.total_potential?.status ||
    row.carbon_total_potential_status ||
    carbon.calculation_state ||
    carbon.calculation_status ||
    row.carbon_calculation_status;
  if (status === "baseline_estimate" || (status === "calculated" && coefficientStatus === "baseline")) {
    return t("baselineEstimate");
  }
  if (status === "draft_estimate" || (status === "calculated" && coefficientStatus === "draft")) {
    return t("draftEstimate");
  }
  if (status === "calculated") {
    return t("calculated");
  }
  if (carbon.evidence_state && carbon.evidence_state !== "ready") {
    return carbonLabel(carbon.evidence_state, t("awaitingEvidence"), t);
  }
  return carbonLabel(status, t("awaitingCoefficients"), t);
}

function carbonAnomalyLabel(value, t) {
  if (!value) {
    return "n/a";
  }
  return statusLabel(value, t);
}

function coefficientSourceLabel(source = {}, t) {
  if (typeof source === "string") {
    if (source === "grid_cell_override" || source === "cell_override") {
      return t("cellOverride");
    }
    if (source === "region_override") {
      return t("regionOverride");
    }
    if (["approved", "baseline", "draft", "global_baseline"].includes(source)) {
      return source === "global_baseline" ? t("globalBaseline") : t("configuredBaseline");
    }
    if (source === "pending_approval" || source === "pending_coefficients") {
      return t("pendingApproval");
    }
  }
  const applied = Array.isArray(source.applied) ? source.applied : [];
  if (!applied.length) {
    return t("notAssigned");
  }
  if (applied.some((item) => String(item).startsWith("grid_cell:"))) {
    return t("cellOverride");
  }
  if (applied.some((item) => String(item).startsWith("region:"))) {
    return t("regionOverride");
  }
  return t("globalBaseline");
}

function carbonMethodologyNotice(carbon, row, t) {
  const coefficientStatus = carbon.coefficient_status || row.carbon_coefficient_status;
  const status =
    carbon.total_potential?.status ||
    row.carbon_total_potential_status ||
    carbon.calculation_status ||
    row.carbon_calculation_status;
  if (coefficientStatus === "baseline" || status === "baseline_estimate") {
    return t("baselineEstimateFieldCalibrationPending");
  }
  if (coefficientStatus === "draft" || status === "draft_estimate") {
    return t("draftEstimatePendingCoefficientApproval");
  }
  return "";
}

function displayValue(value, fallback) {
  if (value === null || value === undefined || value === "") {
    return fallback;
  }
  return String(value);
}

function trendLabel(delta, loading, error, count, t) {
  if (loading) {
    return t("loading");
  }
  if (error) {
    return t("unavailable");
  }
  if (count < 2 || delta === null || delta === undefined) {
    return t("awaitingHistory");
  }
  const sign = delta > 0 ? "+" : "";
  const value = `${sign}${formatNumber(delta)}`;
  if (delta > 0.005) {
    return t("improvingTrend", { value });
  }
  if (delta < -0.005) {
    return t("decliningTrend", { value });
  }
  return t("stableTrend", { value });
}

function ndviHistoryEmptyText(history, t) {
  history = Array.isArray(history) ? history : [];
  if (!history.length) {
    return t("historicalObservationsPending");
  }
  const statuses = Array.from(new Set(history.map((item) => item?.status).filter((status) => status && status !== "ok")));
  if (!statuses.length) {
    return t("historicalObservationsNoNumeric");
  }
  return t("historicalObservationsWithoutValidNdvi", {
    statuses: statuses.map((status) => observationLabel(status, t)).join(", "),
  });
}

function sparklinePoints(values) {
  return values.map((value, index) => `${sparklineX(index, values.length)},${sparklineY(value)}`).join(" ");
}

function sparklineX(index, count) {
  if (count <= 1) {
    return 0;
  }
  return (index / (count - 1)) * 240;
}

function sparklineY(value) {
  // Public NDVI domain used only to position chart pixels, not to classify risk.
  const clamped = Math.max(-1, Math.min(1, Number(value)));
  return 58 - ((clamped + 1) / 2) * 46;
}

function regionLabel(value, t) {
  if (!value) {
    return "n/a";
  }
  return value === "REVIEW-PILOT" ? t("turkeyPilotWorkspace") : value;
}

function carbonValue(value, entry = {}, t) {
  const candidate = value ?? entry.value;
  if (candidate === null || candidate === undefined || candidate === "") {
    return t("awaitingInputs");
  }
  const unit = entry.unit || "tCO2e";
  return `${formatNumber(candidate)} ${unit}`;
}

function carbonAnnualValue(value, entry = {}, fallbackUnit = "tCO2e/ha/year", t) {
  const candidate = value ?? entry.value;
  if (candidate === null || candidate === undefined || candidate === "") {
    return t("awaitingInputs");
  }
  return `${formatNumber(candidate)} ${entry.unit || fallbackUnit}`;
}

function carbonPercent(value, t) {
  if (value === null || value === undefined || value === "") {
    return t("awaitingInputs");
  }
  return `${formatNumber(value)}%`;
}

function carbonNumber(value, t) {
  if (value === null || value === undefined || value === "") {
    return t("awaitingInputs");
  }
  return formatNumber(value);
}

function translateLabel(t, key, fallback, params = {}) {
  if (typeof t !== "function") {
    return fallback;
  }
  const value = t(key, params);
  return value && value !== key ? value : fallback;
}
