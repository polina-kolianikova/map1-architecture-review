import assert from "node:assert/strict";
import test from "node:test";

import { getNdviTrend } from "../src/lib/ndviHistory.js";
import { messages } from "../src/i18n/messages.js";

test("missing, blank, nonfinite and out-of-range values never become points", () => {
  const invalid = [null, undefined, "", "  ", "\t\n", NaN, Infinity, -Infinity,
    "NaN", "Infinity", "invalid", -1.01, 1.01, "1.001", false, true, [], {}];
  const history = invalid.map((ndvi) => ({ ndvi }));
  history.push(null, undefined, {});
  assert.deepEqual(getNdviTrend(history), { points: [], delta: null, messageKey: null });
});

test("real zero, numeric strings and the NDVI range boundaries are retained", () => {
  const history = [-1, " -0.25 ", 0, "0", 1].map((ndvi, index) => ({
    ndvi, acquisition_date: `date-${index}`,
  }));
  const original = structuredClone(history);
  assert.deepEqual(getNdviTrend(history).points, [
    { date: "date-0", ndvi: -1 }, { date: "date-1", ndvi: -0.25 },
    { date: "date-2", ndvi: 0 }, { date: "date-3", ndvi: 0 }, { date: "date-4", ndvi: 1 },
  ]);
  assert.deepEqual(history, original);
});

test("one measured observation alongside null NDVI has no fabricated zero or trend", () => {
  const result = getNdviTrend([
    { acquisition_date: "2026-10-01", ndvi: null, status: "no_sample_pixels" },
    { acquisition_date: "2026-11-01", ndvi: "0.75", status: "ok" },
  ]);
  assert.deepEqual(result.points, [{ date: "2026-11-01", ndvi: 0.75 }]);
  assert.equal(result.delta, null);
  assert.equal(messages.en[result.messageKey],
    "One valid observation is available. A second observation is needed to calculate a trend.");
  assert.doesNotMatch(messages.en[result.messageKey], /no valid NDVI signal/);
});

test("a single genuine zero is one valid observation", () => {
  const result = getNdviTrend([{ acquisition_date: "2026-11-01", ndvi: 0 }]);
  assert.deepEqual(result.points, [{ date: "2026-11-01", ndvi: 0 }]);
  assert.equal(result.delta, null);
  assert.equal(result.messageKey, "ndviHistoryOneObservation");
});

test("two real observations calculate a trend without inserting missing samples", () => {
  const result = getNdviTrend([
    { acquisition_date: "2026-09-01", ndvi: 0.25 },
    { acquisition_date: "2026-10-01", ndvi: null },
    { acquisition_date: "2026-11-01", ndvi: "0" },
  ]);
  assert.deepEqual(result.points, [
    { date: "2026-09-01", ndvi: 0.25 }, { date: "2026-11-01", ndvi: 0 },
  ]);
  assert.equal(result.delta, -0.25);
  assert.equal(result.messageKey, null);
});

test("empty history has neither a point nor a trend", () => {
  for (const history of [[], null, undefined]) {
    assert.deepEqual(getNdviTrend(history), { points: [], delta: null, messageKey: null });
  }
});
