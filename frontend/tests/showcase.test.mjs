import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { messages } from "../src/i18n/messages.js";

test("Final institutional labels retained", () => {
  assert.equal(messages.en.fullPreview, "Active Grid");
  assert.equal(messages.en.baselineDraft, "Baseline Assessment");
  assert.equal(messages.en.carbonAuditEvents_one, "{{formattedCount}} event");
});

test("No supplied Mapbox credential or production scope in defaults", () => {
  const runtime = fs.readFileSync(new URL("../src/config/runtime.js", import.meta.url), "utf8");
  assert.ok(runtime.includes('"REVIEW-SCOPE"'));
  assert.ok(!/[ps]k\.[A-Za-z0-9_-]{15}/.test(runtime));
  assert.equal(fs.readFileSync(new URL("../public/runtime-config.js", import.meta.url), "utf8").trim(), "window.__MAP1_CONFIG__ = window.__MAP1_CONFIG__ || {};");
});

test("Observation palette does not classify values", () => {
  const map = fs.readFileSync(new URL("../src/components/MapPanel.jsx", import.meta.url), "utf8");
  const body = map.match(/function ndviColorExpression\([^)]*\) \{([\s\S]*?)\n\}/)?.[1];
  assert.ok(body?.includes('return "#4f8b80"'));
  assert.ok(!body.includes("interpolate"));
});

test("Omitted service is explained without successful fixture data", () => {
  const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
  assert.ok(html.includes("No production data or map key is included"));
  const client = fs.readFileSync(new URL("../src/api/stage1.js", import.meta.url), "utf8");
  assert.ok(client.includes("Service omitted from architecture sample (HTTP 501)"));
});
