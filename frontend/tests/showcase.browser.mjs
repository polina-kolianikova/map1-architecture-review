import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const base = new URL(process.argv[2] || "http://localhost:5173");
if (base.protocol !== "http:" || base.hostname !== "localhost" || base.username || base.password) {
  throw new Error("This check accepts a local showcase origin only.");
}
const output = path.resolve(process.argv[3] || "../.local-checks/browser");
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const report = { status: "running", pageErrors: [], blockedExternalRequests: [], apiResponses: [] };
try {
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === base.origin) await route.continue();
    else { report.blockedExternalRequests.push(url.origin); await route.abort(); }
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => report.pageErrors.push(error.message));
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith("/api/")) report.apiResponses.push({ path: url.pathname, http: response.status() });
  });
  await page.goto(base.href, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelector(".primary-action")?.innerText.includes("Data unavailable"), undefined, { timeout: 12000 });
  assert.ok(await page.locator(".architecture-review-notice").isVisible());
  assert.match(await page.locator(".architecture-review-notice").innerText(), /private calculations are omitted/);
  assert.ok(await page.locator(".primary-action").isDisabled());
  assert.equal(await page.evaluate(() => window.__MAP1_CONFIG__?.mapboxToken || ""), "");
  assert.ok(report.apiResponses.some((response) => response.path === "/api/stage1/summary" && response.http === 503));
  await page.screenshot({ path: path.join(output, "overview-1600.png") });
  await page.setViewportSize({ width: 390, height: 900 });
  await page.waitForTimeout(200);
  const layout = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
  assert.ok(layout.scrollWidth <= layout.width + 1, JSON.stringify(layout));
  assert.ok(await page.locator(".architecture-review-notice").isVisible());
  await page.screenshot({ path: path.join(output, "overview-390.png") });
  report.mobileLayout = layout;
  assert.deepEqual(report.pageErrors, []);
  assert.deepEqual(report.blockedExternalRequests, []);
  report.status = "pass";
  report.mapboxKeySupplied = false;
  report.fakeCalculationOrFixtureDataSupplied = false;
  report.screenshots = ["overview-1600.png", "overview-390.png"];
} finally {
  await browser.close();
  report.browserClosed = true;
  await fs.writeFile(path.join(output, "browser-report.json"), JSON.stringify(report, null, 2) + "\n");
}
console.log(JSON.stringify(report));
