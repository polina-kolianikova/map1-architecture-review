import { EmptyLine } from "./common";
import { useI18n } from "../hooks/useI18n";
import { cellLabel, formatDate, formatNumber } from "../lib/formatters";
import { statusLabel } from "../lib/status";

export function VerificationTable({ rows, selectedCellUuid, loading, loadFailed, hasFilters, emptyText, onSelect }) {
  const { t } = useI18n();

  if (!rows.length) {
    let message = emptyText || t("noAssetsAvailable");
    if (loading) {
      message = t("loadingData");
    } else if (loadFailed) {
      message = t("dataUnavailableRefresh");
    } else if (hasFilters) {
      message = t("noMatchingAssets");
    }
    return <EmptyLine text={message} />;
  }
  return (
    <table className="verification-table">
      <thead>
        <tr>
          <th scope="col">{t("asset")}</th>
          <th scope="col">{t("carbonReadiness")}</th>
          <th scope="col">{t("evidenceStatus")}</th>
          <th scope="col">{t("area")}</th>
          <th scope="col">{t("lastObservation")}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            aria-selected={row.cell_uuid === selectedCellUuid}
            className={row.cell_uuid === selectedCellUuid ? "is-selected" : ""}
            key={row.id}
            onClick={() => onSelect(row.cell_uuid)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelect(row.cell_uuid);
              }
            }}
            role="button"
            tabIndex={0}
          >
            <td data-label={t("asset")}>
              <span
                aria-current={row.cell_uuid === selectedCellUuid ? "true" : undefined}
                className="cell-link"
              >
                {cellLabel(row.cell_uuid)}
              </span>
            </td>
            <td data-label={t("carbon")}>{carbonReadinessLabel(row, t)}</td>
            <td data-label={t("evidence")}>{statusLabel(row.anomaly_type, t)} / NDVI {formatNumber(row.ndvi)}</td>
            <td data-label={t("area")}>{row.area_ha ? `${formatNumber(row.area_ha)} ha` : t("dataUnavailable")}</td>
            <td data-label={t("lastObservation")}>{formatDate(row.acquisition_date) || formatDate(row.latest_event_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function carbonReadinessLabel(row, t) {
  const status =
    row.carbon_total_potential_status ||
    row.carbon_readiness?.total_potential?.status ||
    row.carbon_readiness?.calculation_state ||
    row.carbon_calculation_status ||
    row.carbon_readiness?.calculation_status;
  const coefficientStatus = row.carbon_readiness?.coefficient_status || row.carbon_coefficient_status;
  if (row.carbon_readiness?.evidence_state && row.carbon_readiness.evidence_state !== "ready") {
    return t("awaitingEvidence");
  }
  if (!status || status === "future_analytics_module" || status === "pending_coefficients") {
    return t("awaitingCoefficients");
  }
  if (status === "baseline_estimate" || (status === "calculated" && coefficientStatus === "baseline")) {
    return t("baselineEstimate");
  }
  if (status === "draft_estimate" || (status === "calculated" && coefficientStatus === "draft")) {
    return t("draftEstimate");
  }
  return {
    calculated: t("anyCalculatedValue"),
    failed: t("failed"),
    pending_coefficients: t("awaitingCoefficients"),
    pending_inputs: t("pendingInputs"),
    skipped_missing_evidence: t("missingEvidence"),
  }[status] || String(status).replaceAll("_", " ");
}
