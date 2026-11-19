function eventCount(value) {
  if ((typeof value !== "number" && typeof value !== "string") || (typeof value === "string" && !value.trim())) {
    return null;
  }
  const count = Number(value);
  return Number.isSafeInteger(count) && count >= 0 ? count : null;
}

export function getAssetAudit(row) {
  // These per-asset counts come from separate verification and carbon ledgers.
  // Nested and flat carbon counts describe the same events; use only one.
  const carbon = row.carbon_readiness || {};
  const verificationCount = eventCount(row.audit_event_count);
  const carbonCount = eventCount(carbon.audit_event_count) ?? eventCount(row.carbon_audit_event_count);
  const totalCount = verificationCount !== null && carbonCount !== null
    ? verificationCount + carbonCount
    : null;

  return {
    verificationCount,
    carbonCount,
    totalCount,
    latestVerificationEventAt: row.latest_event_at,
    latestCarbonEventAt: carbon.latest_event_at || row.latest_carbon_event_at,
  };
}
