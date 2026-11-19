export function withStage5Status(summary, stage5Status) {
  const carbonSummary = stage5Status?.summary;
  const assessedCells = Number(carbonSummary?.carbon_assessments || 0);
  if (!carbonSummary || !Number.isFinite(assessedCells) || assessedCells <= 0) {
    return summary;
  }

  // Carbon classifications provide the legacy display only until the scope has
  // real Layer C results. Keep those results consistent with evidence filters.
  const verificationFallback = Number(summary?.verification_results || 0) > 0
    ? {}
    : {
      verification_results: assessedCells,
      verification_status: [
        { anomaly_type: "anomaly", result_count: Number(carbonSummary.anomaly_cells || 0) },
        { anomaly_type: "watch", result_count: Number(carbonSummary.watch_cells || 0) },
        { anomaly_type: "ok", result_count: Number(carbonSummary.ok_cells || 0) },
        { anomaly_type: "insufficient_data", result_count: Number(carbonSummary.insufficient_data_cells || 0) },
        { anomaly_type: "unverified", result_count: Number(carbonSummary.unverified_cells || 0) },
      ],
    };
  return {
    ...summary,
    ...verificationFallback,
    stage5_carbon: {
      calculated_cells: Number(carbonSummary.calculated_cells || 0),
      skipped_missing_evidence_cells: Number(carbonSummary.skipped_missing_evidence_cells || 0),
      total_potential_tco2e: carbonSummary.total_potential_tco2e,
      net_annual_increment_tco2e_yr: carbonSummary.net_annual_increment_tco2e_yr,
      average_net_annual_increment_tco2e_ha_yr: carbonSummary.average_net_annual_increment_tco2e_ha_yr,
      average_confidence_score: carbonSummary.average_confidence_score,
      latest_calculated_at: carbonSummary.latest_calculated_at,
      methodology_version: stage5Status.methodology_version,
      coefficient_set_version: stage5Status.coefficient_set_version,
      coefficient_status: stage5Status.coefficient_status,
      recalculation_required: Boolean(stage5Status.recalculation_required),
      summary_state: stage5Status.summary_state || carbonSummary.summary_state || "unknown",
      methodology: stage5Status.methodology || null,
      annual_increment: stage5Status.annual_increment || null,
    },
  };
}
