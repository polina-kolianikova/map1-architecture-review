export function withLayerBStatus(summary, stage4Status) {
  const latestRun = stage4Status?.latest_run;
  const totalCells = Number(latestRun?.total_cells || summary?.grid_cells || 0);
  if (!latestRun || !Number.isFinite(totalCells) || totalCells <= 0) return summary;

  // Source run counters can include multiple tasks. The observation summary
  // counts unique current cells and keeps pending work distinct from missing data.
  const validCells = Math.max(0, Math.min(totalCells, Number(summary?.real_sentinel_observations || 0)));
  const hasObservedCount = summary?.sentinel_observations !== null && Number.isFinite(Number(summary?.sentinel_observations));
  const observedCells = hasObservedCount
    ? Math.max(validCells, Math.min(totalCells, Number(summary.sentinel_observations)))
    : null;
  return {
    ...summary,
    stage4_layer_b: {
      cadence_days: stage4Status.cadence_days ?? null,
      failed_cells: Number(latestRun.failed_cells || 0),
      finished_at: latestRun.finished_at || "",
      ndvi_gap_cells: totalCells - validCells,
      ndvi_cells: validCells,
      ndvi_invalid_cells: observedCells === null ? null : observedCells - validCells,
      ndvi_pending_cells: observedCells === null ? null : totalCells - observedCells,
      open_alerts: Number(stage4Status.open_alerts || 0),
      scheduler_enabled: Boolean(stage4Status.scheduler_enabled),
      status: latestRun.status || "",
      total_batches: Number(latestRun.total_batches || 0),
      total_cells: totalCells,
    },
  };
}

export function layerBObservationCounts(layerBStatus) {
  if (!layerBStatus) return [];
  return [
    { status: "ok", count: Number(layerBStatus.ndvi_cells || 0) },
    { status: "no_valid_pixels", count: Number(layerBStatus.ndvi_invalid_cells || 0) },
    { status: "not_observed", count: Number(layerBStatus.ndvi_pending_cells || 0) },
  ].filter((item) => Number.isFinite(item.count) && item.count > 0);
}
