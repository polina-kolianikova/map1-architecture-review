import { expect, test } from "@playwright/test";

const frontendUrl = process.env.FRONTEND_URL || "http://127.0.0.1:5173";
const mapMode = process.env.EXPECT_MAPBOX_READY || "auto";
const vectorTileMode = process.env.EXPECT_VECTOR_TILES || "auto";
const screenshotDir = process.env.SCREENSHOT_DIR || "";
const auth = parseBasicAuth(process.env.FRONTEND_AUTH || "");

if (auth) {
  test.use({ httpCredentials: auth });
}

const viewports = [
  { name: "desktop", width: 1365, height: 900 },
  { name: "desktop-1024", width: 1024, height: 768 },
  { name: "mobile", width: 390, height: 844 },
];

for (const viewport of viewports) {
  test(`Stage 1 renders on ${viewport.name}`, async ({ page }) => {
    test.setTimeout(90000);

    const consoleErrors = [];
    const failedRequests = [];
    const mapboxApiFailures = [];
    const badResponses = [];
    const vectorTileResponses = [];
    const assetRegisterResponses = [];

    page.on("console", (message) => {
      if (message.type() === "error") {
        consoleErrors.push(message.text());
      }
    });
    page.on("pageerror", (error) => consoleErrors.push(error.message));
    page.on("requestfailed", (request) => {
      const url = request.url();
      const failureText = request.failure()?.errorText || "";
      if (url.includes("events.mapbox.com")) {
        return;
      }
      if (url.includes("api.mapbox.com/v4/")) {
        return;
      }
      if (
        url.includes("api.mapbox.com/styles/") &&
        failureText === "net::ERR_ABORTED"
      ) {
        return;
      }
      if (isMvtUrl(url) && failureText === "net::ERR_ABORTED") {
        return;
      }
      if (url.includes("/api/stage2/assets") && failureText === "net::ERR_ABORTED") {
        return;
      }
      if (url.includes("api.mapbox.com")) {
        mapboxApiFailures.push(redactUrl(url));
        return;
      }
      failedRequests.push(`${failureText} ${redactUrl(url)}`.trim());
    });
    page.on("response", (response) => {
      const url = response.url();
      if (isMvtUrl(url) && response.status() === 200) {
        vectorTileResponses.push(redactUrl(url));
      }
      if (url.includes("/api/stage2/assets?") && response.status() === 200) {
        assetRegisterResponses.push(redactUrl(url));
      }
      if (url.includes("/api/") && response.status() >= 400) {
        badResponses.push(`${response.status()} ${redactUrl(url)}`);
      }
    });

    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.addInitScript(() => {
      window.__MAP1_ENABLE_TEST_STATS__ = true;
    });
    await page.goto(frontendUrl, { waitUntil: "domcontentloaded" });

    await expect(page.getByRole("heading", { name: "Map 1 Turkey" })).toBeVisible();
    await expect(page.getByLabel("Current portfolio snapshot")).toBeVisible();
    await expect(page.getByText(/Browser display capped/i)).toHaveCount(0);
    if (viewport.name === "mobile") {
      const statusDistribution = page.getByLabel(/(?:Pilot|Rollout) status distribution/);
      await statusDistribution.scrollIntoViewIfNeeded();
      await expect(statusDistribution).toBeVisible();
    }
    await expect(page.locator(".asset-passport")).toHaveCount(0);
    const workspaceButton = page.getByRole("button", { name: /turkey workspace/i }).first();
    if (await workspaceButton.count()) {
      await expect(workspaceButton).toBeEnabled({ timeout: 30000 });
      await workspaceButton.click();
    }
    await page.locator(".topbar").waitFor({ timeout: 30000 });
    const pilotScopeButton = page.locator(".topbar .scope-option", { hasText: "Pilot" }).first();
    if (await pilotScopeButton.count()) {
      await expect(pilotScopeButton).toBeVisible({ timeout: 30000 });
      if ((await pilotScopeButton.getAttribute("aria-pressed")) !== "true") {
        await expect(page.getByLabel("Project coverage scope")).toContainText("Public forest mask");
        await expect(page.getByRole("status")).toContainText(/Active Grid/i);
        await pilotScopeButton.click();
        await expect(pilotScopeButton).toHaveAttribute("aria-pressed", "true", { timeout: 30000 });
      }
    }
    const evidenceFilter = page.locator(".verification-status-filter").first().locator("select");
    await expect(page.getByLabel("Project coverage scope")).toContainText(/Clean mask|Verified pilot area/, { timeout: 30000 });
    await expect(page.locator(".asset-passport")).toContainText("Select a grid cell or register row");
    await expect(page.locator(".app-shell")).toHaveClass(/theme-dark/);
    const mapModeButton = page.locator('.topbar-actions button[aria-label$="mode"]').first();
    if ((await mapModeButton.getAttribute("aria-pressed")) !== "true") {
      await expect(page.getByRole("button", { name: /^Focus map$/ })).toBeDisabled();
      await mapModeButton.click();
    }
    await expect(mapModeButton).toHaveAttribute("aria-pressed", "true");
    const mapCanvas = page.locator(".map-canvas.is-ready canvas").first();
    let canvasWidthBeforeFocus = 0;
    if (await page.locator(".map-canvas").count()) {
      await expect(page.locator(".map-canvas.is-ready")).toBeVisible({ timeout: 30000 });
      const box = await mapCanvas.boundingBox();
      canvasWidthBeforeFocus = Math.floor(box?.width || 0);
    }
    await expect(page.getByRole("button", { name: /^Focus map$/ })).toBeEnabled();
    await page.getByRole("button", { name: /^Focus map$/ }).click();
    await expect(page.locator(".app-shell")).toHaveClass(/is-map-focus/);
    if (viewport.name === "desktop") {
      await expect.poll(async () => {
        const box = await mapCanvas.boundingBox();
        return Math.floor(box?.width || 0);
      }, { message: "focused map canvas width" }).toBeGreaterThan(1000);
      if (canvasWidthBeforeFocus) {
        await expect.poll(async () => {
          const box = await mapCanvas.boundingBox();
          return Math.floor(box?.width || 0);
        }, { message: "focus expands map canvas" }).toBeGreaterThan(canvasWidthBeforeFocus + 80);
      }
    }
    await expect(page.getByRole("button", { name: /^Exit focus$/ })).toHaveAttribute("aria-pressed", "true");
    await page.getByRole("button", { name: /^Exit focus$/ }).click();
    await expect(page.locator(".app-shell")).not.toHaveClass(/is-map-focus/);
    await mapModeButton.click();
    await expect(mapModeButton).toHaveAttribute("aria-pressed", "false");
    await expect(page.getByRole("button", { name: /^Focus map$/ })).toBeDisabled();
    await mapModeButton.click();
    await expect(mapModeButton).toHaveAttribute("aria-pressed", "true");
    await page.getByRole("button", { name: /^Overview$/ }).click();
    await expect(page.getByLabel("Portfolio overview")).toBeVisible();
    await page.locator(".portfolio-signal-row", { hasText: "OK" }).click();
    await expect(page.getByLabel("Project coverage scope")).toContainText(/Clean mask|Verified pilot area/);
    await expect(evidenceFilter).toHaveValue("ok");
    await evidenceFilter.selectOption("all");
    await expect(evidenceFilter).toHaveValue("all");
    await expect(page.locator(".metric strong").first()).not.toHaveText("...", {
      timeout: 30000,
    });
    await expect(page.locator(".verification-table tbody tr").first()).toBeVisible({ timeout: 30000 });
    await expect(page.getByRole("alert")).toHaveCount(0);

    await expect.poll(() => assetRegisterResponses.length, {
      message: "initial Stage 2 register request",
    }).toBeGreaterThan(0);
    const registerRequestsBeforeRefresh = assetRegisterResponses.length;
    await page.getByRole("button", { name: /refresh/i }).click();
    await expect.poll(() => assetRegisterResponses.length, {
      message: "refresh reloads Stage 2 register",
    }).toBeGreaterThan(registerRequestsBeforeRefresh);
    await waitForRefreshComplete(page);
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(page.locator(".asset-passport")).toContainText("Select a grid cell or register row");

    const metricTexts = await page.locator(".metric strong").allTextContents();
    const metrics = metricTexts.map(parseMetric);
    expect(metrics[0], "grid cells").toBeGreaterThan(0);
    expect(metrics[1], "Layer A rows").toBeGreaterThan(0);
    expect(metrics[2], "NDVI rows").toBeGreaterThan(0);
    expect(parsePercent(metricTexts[3]), "NDVI coverage").toBeGreaterThanOrEqual(90);
    expect(metrics[4], "verification rows").toBe(metrics[0]);
    expect(metrics[5], "audit rows").toBeGreaterThanOrEqual(metrics[4]);
    await expect(page.getByLabel("Project coverage scope")).toContainText(`${metricTexts[0]} pilot cells`);

    const statusSummary = await page.evaluate(async () => {
      const response = await fetch("/api/stage1/summary");
      return response.json();
    });
    const populatedStatus =
      statusSummary.verification_status.find((item) => item.result_count > 0 && item.anomaly_type === "anomaly") ||
      statusSummary.verification_status.find((item) => item.result_count > 0 && item.anomaly_type !== "unverified");
    expect(populatedStatus, "non-empty verification status").toBeTruthy();
    const statusLabels = {
      anomaly: "Anomaly",
      ok: "OK",
      watch: "Watch",
      insufficient_data: "Insufficient data",
    };
    const populatedStatusLabel = statusLabels[populatedStatus.anomaly_type] || populatedStatus.anomaly_type;
    const populatedStatusButton = page.locator(".status-filter-row", { hasText: populatedStatusLabel });
    await populatedStatusButton.click();
    await expect(populatedStatusButton).toHaveAttribute("aria-pressed", "true");
    await expect(evidenceFilter).toHaveValue(populatedStatus.anomaly_type);
    await expect(page.locator(".verification-table tbody tr").first()).toContainText(populatedStatusLabel);
    await populatedStatusButton.click();
    await expect(evidenceFilter).toHaveValue("all");

    const firstCell = page.locator(".verification-table tbody .cell-link").first();
    const secondCell = page.locator(".verification-table tbody .cell-link").nth(1);
    const firstCellText = await firstCell.textContent();
    await secondCell.click();
    const secondCellText = await secondCell.textContent();
    const secondCellId = secondCellText.replace(/^Cell /, "").trim();
    let expectedMapCellId = secondCellId;
    await expect(page.locator(".asset-passport")).toContainText(secondCellText.trim());
    await expect(page.getByLabel("Carbon model inputs")).toContainText("Carbon readiness");
    await expect(page.getByLabel("Carbon model inputs")).toContainText("Total Potential");
    await expect(page.getByLabel("Carbon model inputs")).toContainText("Soil carbon");
    await expect(page.getByLabel("Selected cell popup")).toContainText(secondCellText.trim());
    await page.getByLabel("Selected cell popup").getByRole("button", { name: "Clear selected asset" }).click();
    await expect(page.locator(".asset-passport")).toContainText("Select a grid cell or register row");
    await expect(page.locator(".map-cell-popup")).toHaveCount(0);
    await expect
      .poll(async () => {
        const stats = await page.evaluate(() => window.__map1MapStats || {});
        return stats.selectedCell || "";
      })
      .toBe("");
    await secondCell.click();
    await expect(page.locator(".asset-passport")).toContainText(secondCellText.trim());
    await expect(page.getByLabel("Selected cell popup")).toContainText(secondCellText.trim());
    await expect(page.locator(".audit-list")).toContainText(secondCellId.slice(0, 8));
    await expect
      .poll(async () => {
        const stats = await page.evaluate(() => window.__map1MapStats || {});
        return stats.selectedCell || "";
      })
      .toContain(secondCellId);
    const nextButton = page.getByRole("button", { name: "Next" });
    if (await nextButton.isEnabled()) {
      await nextButton.click();
      await expect(page.locator(".verification-pager")).toContainText("page 2 of");
      await waitForFirstCellText(page, (value) => value !== firstCellText.trim());
    } else {
      await expect(page.locator(".verification-pager")).toContainText("page 1 of 1");
    }
    await expect
      .poll(async () => {
        const stats = await page.evaluate(() => window.__map1MapStats || {});
        return stats.selectedCell || "";
      })
      .toContain(expectedMapCellId);
    await page.getByPlaceholder("Short ID or UUID").fill(secondCellText.replace(/^Cell /, "").trim());
    expectedMapCellId = secondCellId;
    await expect(page.locator(".verification-table tbody .cell-link").first()).toHaveText(
      secondCellText.trim()
    );
    await page.getByPlaceholder("Short ID or UUID").fill("not-a-real-cell");
    await expect(page.getByPlaceholder("Short ID or UUID")).toHaveValue("not-a-real-cell");
    await expect(page.locator(".bottom-panel")).toContainText("No matching assets");
    await expect(page.locator(".asset-passport")).toContainText("Select a grid cell or register row");
    await page.getByPlaceholder("Short ID or UUID").fill("");
    await expect(page.locator(".verification-table tbody .cell-link").first()).toBeVisible();
    const firstCellAfterClear = await page.locator(".verification-table tbody .cell-link").first().textContent();
    await expect(page.locator(".asset-passport")).toContainText("Select a grid cell or register row");
    await page.locator(".verification-table tbody .cell-link").first().click();
    expectedMapCellId = firstCellAfterClear.replace(/^Cell /, "").trim();
    await expect(page.locator(".asset-passport")).toContainText(firstCellAfterClear.trim());
    await evidenceFilter.selectOption("unverified");
    await expect(page.locator(".status-filter-row", { hasText: "No result" })).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator(".bottom-panel")).toContainText("No matching assets");
    await expect(page.locator(".asset-passport")).toContainText("Select a grid cell or register row");
    await evidenceFilter.selectOption("all");
    const firstCellAfterAll = await page.locator(".verification-table tbody .cell-link").first().textContent();
    await page.locator(".verification-table tbody .cell-link").first().click();
    expectedMapCellId = firstCellAfterAll.replace(/^Cell /, "").trim();

    await expect(page.getByRole("heading", { name: "Satellite signal" })).toBeVisible();
    await expect(page.locator(".observation-list")).toContainText("Valid Sentinel NDVI");

    const mapCanvasCount = await page.locator(".map-canvas").count();
    const shouldUseMapbox = mapMode === "1" || (mapMode === "auto" && mapCanvasCount > 0);

    if (shouldUseMapbox) {
      await expect(page.locator(".map-canvas.is-ready")).toBeVisible({ timeout: 20000 });
      await expect(page.locator(".mapboxgl-ctrl-attrib")).toBeVisible();
      await expect(page.locator(".map-fallback")).toHaveCount(0);
      await expect.poll(async () => {
        const box = await page.locator(".map-canvas.is-ready canvas").first().boundingBox();
        return Math.floor(box?.width || 0);
      }, { message: "Mapbox canvas width" }).toBeGreaterThan(100);
      await expect.poll(async () => {
        const box = await page.locator(".map-canvas.is-ready canvas").first().boundingBox();
        return Math.floor(box?.height || 0);
      }, { message: "Mapbox canvas height" }).toBeGreaterThan(100);
      const canvasBox = await page.locator(".map-canvas.is-ready canvas").first().boundingBox();
      expect(canvasBox?.width || 0, "Mapbox canvas width").toBeGreaterThan(100);
      expect(canvasBox?.height || 0, "Mapbox canvas height").toBeGreaterThan(100);
      const mapStats = await page.evaluate(() => window.__map1MapStats || {});
      expect(mapStats.live, "live map flag").toBe(true);
      expect(Number.isFinite(mapStats.zoom), "map zoom telemetry").toBe(true);
      await page.waitForTimeout(650);
      const initialZoom = await page.evaluate(() => window.__map1MapStats?.zoom || 0);
      expect(initialZoom, "selected asset fit zoom").toBeGreaterThan(13);
      await page.getByRole("button", { name: "Zoom out" }).click();
      await expect
        .poll(
          async () => {
            const stats = await page.evaluate(() => window.__map1MapStats || {});
            return stats.zoom || 0;
          },
          { message: "Mapbox zoom out control" }
        )
        .toBeLessThan(initialZoom - 0.05);
      const zoomAfterOut = await page.evaluate(() => window.__map1MapStats?.zoom || 0);
      await page.getByRole("button", { name: "Zoom in" }).click();
      await expect
        .poll(
          async () => {
            const stats = await page.evaluate(() => window.__map1MapStats || {});
            return stats.zoom || 0;
          },
          { message: "Mapbox zoom in control" }
        )
        .toBeGreaterThan(zoomAfterOut + 0.05);
      if (vectorTileMode === "1") {
        await expect.poll(
          async () => Boolean((await page.evaluate(() => window.__map1MapStats || {})).vectorTiles),
          { message: "vector tile path" }
        ).toBe(true);
        expect(vectorTileResponses.length, "loaded vector tiles").toBeGreaterThan(0);
        await expect.poll(
          () => vectorTileResponses.some((url) => url.includes("/stage2/tiles/layer-c/")),
          { message: "loaded Layer C vector tiles", timeout: 30000 }
        ).toBe(true);
      }
      expect(mapStats.gridFeatures || 0, "grid features on map").toBeGreaterThan(0);
      expect(mapStats.layerAFeatures || 0, "Layer A features on map").toBeGreaterThan(0);
      expect(mapStats.layerBFeatures || 0, "Layer B features on map").toBeGreaterThan(0);
      expect(mapStats.layerCFeatures || 0, "Layer C features on map").toBeGreaterThan(0);
      expect(mapStats.selectedCell || "", "selected cell on map").toContain(expectedMapCellId);
      const anomalyCount = Number(
        (await page.locator(".status-filter-row", { hasText: "Anomaly" }).locator("b").textContent()) || "0"
      );
      await page.locator(".status-filter-row", { hasText: "Anomaly" }).click();
      await expect(evidenceFilter).toHaveValue("anomaly");
      await expect.poll(async () => {
        const stats = await page.evaluate(() => window.__map1MapStats || {});
        return stats.gridFeatures || 0;
      }, { message: "filtered map grid feature count" }).toBe(anomalyCount);
      await expect.poll(async () => {
        const stats = await page.evaluate(() => window.__map1MapStats || {});
        return stats.sourceGridFeatures || 0;
      }, { message: "filtered Mapbox source feature count" }).toBe(anomalyCount);
      await expect(page.locator(".map-scope-badge").first()).toContainText(
        `${anomalyCount} shown of`
      );
      await page.getByRole("button", { name: "Fit area" }).click();
      await expect(evidenceFilter).toHaveValue("anomaly");
      await page.locator(".status-filter-row", { hasText: "Anomaly" }).click();
      await expect(evidenceFilter).toHaveValue("all");
      await waitForRefreshComplete(page);
      await expect(page.locator(".verification-pager")).toContainText("assets");
      await expect(page.locator(".verification-pager")).not.toContainText("No matching assets");
    } else {
      expect(mapMode, "map mode").not.toBe("1");
      await expect(page.locator(".map-canvas")).toHaveCount(0);
      await expect(page.locator(".mapboxgl-ctrl-attrib")).toHaveCount(0);
      await expect(page.locator(".static-map svg").first()).toBeVisible({ timeout: 30000 });
      const anomalyCount = Number(
        (await page.locator(".status-filter-row", { hasText: "Anomaly" }).locator("b").textContent()) || "0"
      );
      await page.locator(".status-filter-row", { hasText: "Anomaly" }).click();
      await expect(page.locator(".static-map .grid-path")).toHaveCount(anomalyCount);
      await page.locator(".status-filter-row", { hasText: "Anomaly" }).click();
    }

    await page.evaluate(() => window.scrollTo(0, 0));
    const layout = await page.evaluate(() => {
      const map = document.querySelector(".map-area")?.getBoundingClientRect();
      const sidebar = document.querySelector(".sidebar")?.getBoundingClientRect();
      return {
        bodyWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
        mapTop: map?.top ?? 0,
        sidebarTop: sidebar?.top ?? 0,
      };
    });
    expect(layout.bodyWidth, "horizontal overflow").toBeLessThanOrEqual(layout.viewportWidth + 1);
    if (viewport.name === "mobile") {
      expect(layout.mapTop, "mobile map position").toBeLessThan(layout.sidebarTop);
    }

    if (screenshotDir) {
      await page.screenshot({
        path: `${screenshotDir}/stage1-${viewport.name}.png`,
        fullPage: true,
      });
    }

    expect(failedRequests, "failed requests").toEqual([]);
    expect(mapboxApiFailures, "Mapbox API failures").toEqual([]);
    expect(badResponses, "bad API responses").toEqual([]);
    expect(consoleErrors, "console errors").toEqual([]);
  });
}

async function waitForRefreshComplete(page) {
  await expect(page.getByRole("button", { name: /^Refresh$/ })).toBeEnabled({
    timeout: 30000,
  });
  await expect(page.locator(".verification-table tbody .cell-link").first()).toBeVisible({
    timeout: 30000,
  });
}

async function waitForFirstCellText(page, predicate) {
  const handle = await page.waitForFunction(
    ({ selector }) => {
      const text = document.querySelector(selector)?.textContent?.trim() || "";
      return text || null;
    },
    { selector: ".verification-table tbody .cell-link" },
    { timeout: 30000 }
  );
  const firstText = await handle.jsonValue();
  if (predicate(firstText)) {
    return firstText;
  }

  const changedHandle = await page.waitForFunction(
    ({ selector, previousText }) => {
      const text = document.querySelector(selector)?.textContent?.trim() || "";
      return text && text !== previousText ? text : null;
    },
    { selector: ".verification-table tbody .cell-link", previousText: firstText },
    { timeout: 30000 }
  );
  return await changedHandle.jsonValue();
}

function parseMetric(value) {
  const metric = Number(String(value).replace(/,/g, "").replace(/%$/, ""));
  return Number.isFinite(metric) ? metric : 0;
}

function parsePercent(value) {
  return parseMetric(value);
}

function parseBasicAuth(value) {
  if (!value) {
    return null;
  }
  const separator = value.indexOf(":");
  if (separator < 1) {
    throw new Error("FRONTEND_AUTH must use username:password format");
  }
  return {
    username: value.slice(0, separator),
    password: value.slice(separator + 1),
  };
}

function isMvtUrl(value) {
  try {
    return new URL(value).pathname.endsWith(".mvt");
  } catch {
    return false;
  }
}

function redactUrl(value) {
  try {
    const url = new URL(value);
    url.search = "";
    return url.toString();
  } catch {
    return String(value).split("?")[0];
  }
}
