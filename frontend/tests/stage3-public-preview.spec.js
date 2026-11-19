import { expect, test } from "@playwright/test";

const frontendUrl = process.env.FRONTEND_URL || "http://127.0.0.1:5173";
const auth = parseBasicAuth(process.env.FRONTEND_AUTH || "");

if (auth) {
  test.use({ httpCredentials: auth });
}

test.skip(process.env.EXPECT_PUBLIC_PREVIEW !== "1", "set EXPECT_PUBLIC_PREVIEW=1 for live V9 preview QA");

const V9_REGION_CODE = process.env.STAGE3_QA_REGION_CODE || "TR-STAGE3-CLIENT-CLEAN-PILOT-DEVREKANI";
const V9_GRID_VERSION = process.env.STAGE3_QA_GRID_VERSION || "stage3-client-clean-pilot-devrekani-union-v5";
const V9_SCOPE_LABEL = process.env.STAGE3_QA_SCOPE_LABEL || "Client pilot";
const V9_SCOPE_BADGE = process.env.STAGE3_QA_SCOPE_BADGE || V9_SCOPE_LABEL;
const visualCheckpoints = (process.env.STAGE3_QA_CHECKPOINTS
  ? JSON.parse(process.env.STAGE3_QA_CHECKPOINTS)
  : [
  { name: "devrekani-left-visual-forest", center: [33.740924, 41.595025] },
  { name: "devrekani-right-grid-area", center: [33.96, 41.6] },
  { name: "devrekani-north-forest", center: [33.88, 41.68] },
  { name: "devrekani-south-grid-top", center: [33.8, 41.66] },
]);
const emptyCheckpoints = process.env.STAGE3_QA_EMPTY_CHECKPOINTS
  ? JSON.parse(process.env.STAGE3_QA_EMPTY_CHECKPOINTS)
  : [
  { name: "devrekani-town", center: [33.84, 41.6] },
  { name: "seyidiler-road-town", center: [33.72, 41.62] },
  { name: "south-public-nonforest-gap", center: [33.78, 41.52] },
];

test("Stage 3 public preview renders real mask and grid tiles", async ({ page }, testInfo) => {
  test.setTimeout(120000);

  const maskTileResponses = [];
  const gridTileResponses = [];
  const badResponses = [];
  page.on("response", (response) => {
    const url = response.url();
    if (url.includes("/api/stage2/tiles/mask/") && response.status() === 200) {
      maskTileResponses.push(redactUrl(url));
    }
    if (url.includes("/api/stage2/tiles/grid/") && response.status() === 200) {
      gridTileResponses.push(redactUrl(url));
    }
    if (url.includes("/api/") && response.status() >= 400) {
      badResponses.push(`${response.status()} ${redactUrl(url)}`);
    }
  });

  await page.setViewportSize({ width: 1365, height: 900 });
  await page.addInitScript(() => {
    window.__MAP1_ENABLE_TEST_STATS__ = true;
  });
  await page.goto(frontendUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: /turkey workspace/i }).first().click();
  await page.locator(".topbar").waitFor({ timeout: 30000 });

  const fullPreviewButton = page.locator(".topbar .scope-option", { hasText: V9_SCOPE_LABEL }).first();
  if (await fullPreviewButton.count()) {
    if ((await fullPreviewButton.getAttribute("aria-pressed")) !== "true") {
      await fullPreviewButton.click();
    }
    await expect(fullPreviewButton).toHaveAttribute("aria-pressed", "true", { timeout: 30000 });
  }
  await expect(page.getByLabel("Project coverage scope")).toContainText(V9_SCOPE_BADGE, { timeout: 30000 });
  const summary = await page.evaluate(
    async ({ regionCode, gridVersion }) => {
      const params = new URLSearchParams({ region_code: regionCode, grid_version: gridVersion });
      const response = await fetch(`/api/stage1/summary?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`summary failed: ${response.status}`);
      }
      return response.json();
    },
    { regionCode: V9_REGION_CODE, gridVersion: V9_GRID_VERSION }
  );
  expect(summary.region_code).toBe(V9_REGION_CODE);
  expect(summary.grid_version).toBe(V9_GRID_VERSION);
  expect(summary.grid_cells).toBeGreaterThan(0);
  badResponses.length = 0;

  const mapModeButton = page.locator('.topbar-actions button[aria-label$="mode"]').first();
  if ((await mapModeButton.getAttribute("aria-pressed")) !== "true") {
    await mapModeButton.click();
  }
  await expect(page.locator(".map-canvas.is-ready")).toBeVisible({ timeout: 30000 });
  await page.getByRole("button", { name: "Fit area" }).click();

  await expect.poll(
    async () => {
      const stats = await page.evaluate(() => window.__map1MapStats || {});
      return (stats.maskRenderedFeatures || 0) + (stats.maskSourceFeatures || 0);
    },
    { message: "public mask vector features rendered", timeout: 30000 }
  ).toBeGreaterThan(0);
  const scopedMaskTileResponses = maskTileResponses.filter((url) => url.includes(`region_code=${V9_REGION_CODE}`));
  expect(scopedMaskTileResponses.length, "scoped mask tile responses").toBeGreaterThan(0);
  await page.screenshot({ fullPage: true, path: testInfo.outputPath("stage3-public-v10-overview.png") });

  await page.evaluate(({ center }) => {
    const map = window.__map1Map;
    if (map) {
      map.jumpTo({ center, zoom: 12.4 });
    }
  }, visualCheckpoints[0]);
  await page.waitForTimeout(900);
  await expect.poll(
    async () => {
      return page.evaluate(() => {
        const map = window.__map1Map;
        if (!map) {
          return 0;
        }
        const center = map.project(map.getCenter());
        return map.queryRenderedFeatures(center, { layers: ["stage2-grid-tile-fill"] }).length;
      });
    },
    { message: "actual grid feature rendered at checkpoint center z12+", timeout: 45000 }
  ).toBeGreaterThan(0);
  const scopedGridTileResponses = gridTileResponses.filter(
    (url) => url.includes(`region_code=${V9_REGION_CODE}`) && url.includes(`grid_version=${V9_GRID_VERSION}`)
  );
  expect(scopedGridTileResponses.length, "scoped grid tile responses").toBeGreaterThan(0);

  for (const checkpoint of visualCheckpoints) {
    await page.evaluate(({ center }) => {
      const map = window.__map1Map;
      if (map) {
        map.jumpTo({ center, zoom: 12.4 });
      }
    }, checkpoint);
    await page.waitForTimeout(900);
    await expect.poll(
      async () => {
        return page.evaluate(() => {
          const map = window.__map1Map;
          if (!map) {
            return 0;
          }
          const center = map.project(map.getCenter());
          return map.queryRenderedFeatures(center, { layers: ["stage2-grid-tile-fill"] }).length;
        });
      },
      { message: `${checkpoint.name} grid is rendered at center`, timeout: 30000 }
    ).toBeGreaterThan(0);
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath(`stage3-public-v10-${checkpoint.name}.png`),
    });
  }

  for (const checkpoint of emptyCheckpoints) {
    await page.evaluate(({ center }) => {
      const map = window.__map1Map;
      if (map) {
        map.jumpTo({ center, zoom: 15.2 });
      }
    }, checkpoint);
    await page.waitForTimeout(900);
    const renderedAtCenter = await page.evaluate(() => {
      const map = window.__map1Map;
      if (!map) {
        return -1;
      }
      const center = map.project(map.getCenter());
      return map.queryRenderedFeatures(center, { layers: ["stage2-grid-tile-fill"] }).length;
    });
    expect(renderedAtCenter, `${checkpoint.name} has no grid at center`).toBe(0);
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath(`stage3-public-v10-empty-${checkpoint.name}.png`),
    });
  }

  await page.evaluate(({ center }) => {
    const map = window.__map1Map;
    if (map) {
      map.jumpTo({ center, zoom: 15.2 });
    }
  }, visualCheckpoints[0]);
  await page.waitForTimeout(900);

  let clickTarget = null;
  await expect.poll(
    async () => {
      clickTarget = await page.evaluate(() => {
        const map = window.__map1Map;
        if (!map) {
          return null;
        }
        const point = map.project(map.getCenter());
        const feature = map
          .queryRenderedFeatures(point, { layers: ["stage2-grid-tile-fill"] })
          .find((item) => item.properties?.uuid);
        if (!feature) {
          return null;
        }
        return {
          areaHa: Number(feature.properties.area_ha || feature.properties.areaHa || 0),
          uuid: feature.properties.uuid,
          x: map.getContainer().getBoundingClientRect().left + point.x,
          y: map.getContainer().getBoundingClientRect().top + point.y,
        };
      });
      return clickTarget?.uuid || "";
    },
    { message: "rendered grid feature uuid", timeout: 30000 }
  ).toBeTruthy();
  expect(clickTarget?.uuid, "rendered grid feature uuid").toBeTruthy();
  expect(clickTarget.areaHa, "rendered grid feature area").toBeGreaterThan(0);
  const assetResponsePromise = page.waitForResponse(
    (response) =>
      response.url().includes(`/api/stage2/assets/${clickTarget.uuid}`) &&
      response.url().includes(`region_code=${V9_REGION_CODE}`) &&
      response.url().includes(`grid_version=${V9_GRID_VERSION}`) &&
      response.status() === 200,
    { timeout: 30000 }
  );
  await page.mouse.click(clickTarget.x, clickTarget.y);
  const assetResponse = await assetResponsePromise;
  const asset = await assetResponse.json();
  expect(asset.cell_uuid).toBe(clickTarget.uuid);
  await expect(page.locator(".asset-passport, .map-cell-popup").filter({ hasText: clickTarget.uuid })).toBeVisible({
    timeout: 30000,
  });
  expect(badResponses, "API errors").toEqual([]);
  testInfo.annotations.push({
    type: "stage3-client-pilot-qa",
    description: JSON.stringify({
      frontendUrl,
      regionCode: V9_REGION_CODE,
      gridVersion: V9_GRID_VERSION,
      scopeLabel: V9_SCOPE_LABEL,
      scopeBadge: V9_SCOPE_BADGE,
      visualCheckpoints,
      emptyCheckpoints,
      clickedCellUuid: clickTarget.uuid,
    }),
  });
});

function parseBasicAuth(value) {
  if (!value || !value.includes(":")) {
    return null;
  }
  const [username, ...rest] = value.split(":");
  return { username, password: rest.join(":") };
}

function redactUrl(url) {
  try {
    const parsed = new URL(url);
    parsed.searchParams.delete("token");
    return parsed.toString();
  } catch {
    return url;
  }
}
