import assert from "node:assert/strict";
import test from "node:test";

import { getAssetAudit } from "../src/lib/assetAudit.js";

test("asset audit combines distinct ledgers and counts nested/flat carbon events once", () => {
  assert.deepEqual(getAssetAudit({
    audit_event_count: 2,
    latest_event_at: "2026-11-17T12:00:00Z",
    carbon_audit_event_count: 1,
    latest_carbon_event_at: "2026-11-18T12:00:00Z",
    carbon_readiness: { audit_event_count: 1, latest_event_at: "2026-11-18T12:00:00Z" },
  }), {
    verificationCount: 2, carbonCount: 1, totalCount: 3,
    latestVerificationEventAt: "2026-11-17T12:00:00Z",
    latestCarbonEventAt: "2026-11-18T12:00:00Z",
  });
});

test("real zero, string counts and flat-only carbon records retain their values", () => {
  for (const zero of [0, "0"]) {
    const result = getAssetAudit({ audit_event_count: zero, carbon_audit_event_count: zero });
    assert.equal(result.totalCount, 0);
    assert.equal(result.verificationCount, 0);
    assert.equal(result.carbonCount, 0);
  }
  assert.equal(getAssetAudit({ audit_event_count: "2", carbon_audit_event_count: "1" }).totalCount, 3);
  assert.equal(getAssetAudit({ audit_event_count: 2, carbon_audit_event_count: 1,
    carbon_readiness: { audit_event_count: "" } }).totalCount, 3,
  "An unavailable nested count must preserve a known flat count");
  assert.equal(getAssetAudit({
    audit_event_count: 2, carbon_audit_event_count: 7,
    carbon_readiness: { audit_event_count: 0 },
  }).totalCount, 2, "Nested zero is authoritative, rather than a missing count");
});

test("missing or invalid audit counts stay unknown without discarding the known ledger", () => {
  for (const missing of [undefined, null, "", " ", "invalid", NaN, Infinity, -1, 1.5, true, {}]) {
    const verificationMissing = getAssetAudit({ audit_event_count: missing, carbon_audit_event_count: 1 });
    assert.equal(verificationMissing.verificationCount, null);
    assert.equal(verificationMissing.carbonCount, 1);
    assert.equal(verificationMissing.totalCount, null);
    const carbonMissing = getAssetAudit({ audit_event_count: 2, carbon_audit_event_count: missing });
    assert.equal(carbonMissing.verificationCount, 2);
    assert.equal(carbonMissing.carbonCount, null);
    assert.equal(carbonMissing.totalCount, null);
  }
});
