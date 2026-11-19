import { useI18n } from "../hooks/useI18n";

export function VerificationToolbar({
  carbonStatus = "all",
  filter,
  hasActiveFilters = false,
  observedFrom = "",
  observedTo = "",
  status,
  signalStatus = "all",
  page,
  pageCount,
  hasExactTotal = true,
  hasMore = false,
  resultCount,
  visibleCount = 0,
  onCarbonStatusChange,
  onClearFilters,
  onFilterChange,
  onObservedFromChange,
  onObservedToChange,
  onSignalStatusChange,
  onStatusChange,
  onPreviousPage,
  onNextPage,
}) {
  const { t } = useI18n();
  const invalidObservedRange = Boolean(observedFrom && observedTo && observedFrom > observedTo);

  return (
    <div className="verification-toolbar">
      <label className="verification-search">
        <span>{t("searchCell")}</span>
        <input
          value={filter}
          placeholder={t("shortIdOrUuid")}
          onChange={(event) => onFilterChange(event.target.value)}
        />
      </label>
      <label className="verification-status-filter">
        <span>{t("evidenceFilter")}</span>
        <select value={status} onChange={(event) => onStatusChange(event.target.value)}>
          <option value="all">{t("all")}</option>
          <option value="ok">OK</option>
          <option value="watch">{t("watch")}</option>
          <option value="anomaly">{t("anomaly")}</option>
          <option value="insufficient_data">{t("status.insufficientData")}</option>
          <option value="unverified">{t("noResult")}</option>
        </select>
      </label>
      <label className="verification-status-filter">
        <span>{t("signal")}</span>
        <select value={signalStatus} onChange={(event) => onSignalStatusChange(event.target.value)}>
          <option value="all">{t("all")}</option>
          <option value="ok">{t("validNdvi")}</option>
          <option value="no_ndvi">{t("noNdvi")}</option>
          <option value="no_valid_pixels">{t("noValidPixels")}</option>
          <option value="request_failed">{t("requestFailed")}</option>
          <option value="unknown">{t("unknown")}</option>
        </select>
      </label>
      <label className="verification-status-filter">
        <span>{t("carbon")}</span>
        <select value={carbonStatus} onChange={(event) => onCarbonStatusChange(event.target.value)}>
          <option value="all">{t("all")}</option>
          <option value="awaiting_coefficients">{t("awaitingCoefficientsOption")}</option>
          <option value="pending_coefficients">{t("pendingCoefficients")}</option>
          <option value="pending_inputs">{t("pendingInputs")}</option>
          <option value="baseline_estimate">{t("baselineEstimate")}</option>
          <option value="draft_estimate">{t("draftEstimate")}</option>
          <option value="calculated">{t("anyCalculatedValue")}</option>
          <option value="skipped_missing_evidence">{t("missingEvidence")}</option>
          <option value="failed">{t("failed")}</option>
        </select>
      </label>
      <label className="verification-status-filter">
        <span>{t("observedFrom")}</span>
        <input
          type="date"
          value={observedFrom}
          max={observedTo || undefined}
          aria-invalid={invalidObservedRange || undefined}
          onChange={(event) => onObservedFromChange(event.target.value)}
        />
      </label>
      <label className="verification-status-filter">
        <span>{t("observedTo")}</span>
        <input
          type="date"
          value={observedTo}
          min={observedFrom || undefined}
          aria-invalid={invalidObservedRange || undefined}
          onChange={(event) => onObservedToChange(event.target.value)}
        />
      </label>
      {invalidObservedRange && (
        <div className="verification-filter-error" role="alert">
          {t("startDateBeforeEndDate")}
        </div>
      )}
      <button
        className="verification-clear"
        type="button"
        onClick={onClearFilters}
        disabled={!hasActiveFilters}
      >
        {t("clear")}
      </button>
      <div className="verification-pager" aria-label={t("assetRegisterPages")}>
        <button type="button" onClick={onPreviousPage} disabled={page <= 0}>
          {t("previous")}
        </button>
        <span>
          {hasExactTotal
            ? t("assetRows", {
                shown: visibleCount,
                total: resultCount,
                page: page + 1,
                pageCount,
              })
            : t("assetRowsApprox", {
                count: `${resultCount}+`,
                page: page + 1,
                pageCount: hasMore ? `${pageCount}+` : pageCount,
              })}
        </span>
        <button type="button" onClick={onNextPage} disabled={!hasMore && page >= pageCount - 1}>
          {t("next")}
        </button>
      </div>
    </div>
  );
}
