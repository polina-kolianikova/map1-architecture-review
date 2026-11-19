export function getNdviTrend(history = []) {
  const points = [];
  for (const item of Array.isArray(history) ? history : []) {
    const value = item?.ndvi;
    if (typeof value !== "number" && typeof value !== "string") continue;
    if (typeof value === "string" && !value.trim()) continue;
    const ndvi = Number(value);
    if (!Number.isFinite(ndvi) || ndvi < -1 || ndvi > 1) continue;
    points.push({ date: item.acquisition_date, ndvi });
  }
  return {
    points,
    delta: points.length >= 2 ? points.at(-1).ndvi - points[0].ndvi : null,
    messageKey: points.length === 1 ? "ndviHistoryOneObservation" : null,
  };
}
