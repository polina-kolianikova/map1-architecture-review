import fs from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

const frontendUrl = process.env.FRONTEND_URL || "http://127.0.0.1:5173";
const authValue = process.env.FRONTEND_AUTH || "";
const auth = parseBasicAuth(authValue);
const expectBasicAuth = process.env.EXPECT_BASIC_AUTH === "1" || Boolean(auth);
const screenshotDir = process.env.SCREENSHOT_DIR || "";

const viewports = [
  ["desktop", { width: 1365, height: 900 }],
  ["tablet", { width: 900, height: 1100 }],
  ["mobile", { width: 390, height: 844 }],
];

test("Stage 6 review access boundary", async ({ playwright }, testInfo) => {
  const unauthenticated = await playwright.request.newContext({ ignoreHTTPSErrors: false });
  const unauthenticatedRoot = await unauthenticated.get(frontendUrl);
  const unauthenticatedHealth = await unauthenticated.get(new URL("/api/health", frontendUrl).toString());
  const unauthenticatedLocalization = await unauthenticated.get(
    new URL("/api/meta/localization", frontendUrl).toString()
  );
  const proof = {
    frontend_url: sanitizedFrontendUrl(),
    expect_basic_auth: expectBasicAuth,
    auth_provided: Boolean(authValue),
    unauthenticated: {
      root: unauthenticatedRoot.status(),
      health: unauthenticatedHealth.status(),
      localization: unauthenticatedLocalization.status(),
    },
    authenticated: null,
  };

  if (expectBasicAuth) {
    expect(unauthenticatedRoot.status()).toBe(401);
    expect(unauthenticatedHealth.status()).toBe(401);
    expect(unauthenticatedLocalization.status()).toBe(401);
  } else {
    expect(unauthenticatedRoot.status()).toBeLessThan(400);
  }

  await unauthenticated.dispose();

  if (!authValue) {
    writeAccessProof(testInfo, proof);
    if (expectBasicAuth) {
      throw new Error("FRONTEND_AUTH is required when EXPECT_BASIC_AUTH=1");
    }
    return;
  }

  const authenticated = await playwright.request.newContext({
    extraHTTPHeaders: {
      Authorization: `Basic ${Buffer.from(authValue).toString("base64")}`,
    },
    ignoreHTTPSErrors: false,
  });
  const authenticatedRoot = await authenticated.get(frontendUrl);
  const authenticatedHealth = await authenticated.get(new URL("/api/health", frontendUrl).toString());
  const localizationResponse = await authenticated.get(new URL("/api/meta/localization", frontendUrl).toString());
  proof.authenticated = {
    root: authenticatedRoot.status(),
    health: authenticatedHealth.status(),
    localization: localizationResponse.status(),
  };
  expect(authenticatedRoot.status()).toBe(200);
  expect(authenticatedHealth.status()).toBe(200);
  expect(localizationResponse.status()).toBe(200);
  const localization = await localizationResponse.json();
  expect(localization.default_locale).toBe("en");
  expect(localization.supported_locales).toContain("en");
  await authenticated.dispose();
  writeAccessProof(testInfo, proof);
});

for (const [name, viewport] of viewports) {
  test(`Stage 6 live workflow on ${name}`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({
      httpCredentials: auth || undefined,
      ignoreHTTPSErrors: false,
      viewport,
    });
    const page = await context.newPage();
    const consoleErrors = [];
    const badResponses = [];

    page.on("console", (message) => {
      if (message.type() === "error") {
        consoleErrors.push(message.text());
      }
    });
    page.on("pageerror", (error) => consoleErrors.push(error.message));
    page.on("response", (response) => {
      if (response.url().includes("/api/") && response.status() >= 400) {
        badResponses.push(`${response.status()} ${response.url().split("?")[0]}`);
      }
    });

    await page.goto(frontendUrl, { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expect(page.getByRole("heading", { name: "Map 1 Turkey" })).toBeVisible();
    await expect(page.getByLabel("Portfolio overview")).toBeVisible();
    await expect(page.getByText("Carbon Readiness").first()).toBeVisible();
    const overflowingOverviewStats = await page.locator(".global-stats strong").evaluateAll((nodes) =>
      nodes
        .filter((node) => node.scrollWidth > node.clientWidth + 1)
        .map((node) => node.textContent)
    );
    expect(overflowingOverviewStats).toEqual([]);
    await expect(page.locator("body")).not.toContainText(/[\u0400-\u04FF]/);
    await expect(page.locator("body")).not.toContainText(/Calculation Protocol|Methodology \/ Calculation Protocol/i);

    const localization = await page.evaluate(async () => {
      const response = await fetch("/api/meta/localization");
      return { status: response.status, body: await response.json() };
    });
    expect(localization.status).toBe(200);
    expect(localization.body.default_locale).toBe("en");
    expect(localization.body.supported_locales).toContain("en");

    await screenshot(page, testInfo, `${name}-01-overview.png`);

    await page.getByRole("button", { name: /turkey workspace/i }).first().click();
    await expect(page.locator(".topbar")).toBeVisible({ timeout: 30000 });
    await expect(page.getByText(/Rollout totals|Pilot totals/).first()).toBeVisible({ timeout: 30000 });
    await expect(page.getByRole("button", { name: "Refresh" })).toBeVisible({ timeout: 30000 });
    await screenshot(page, testInfo, `${name}-02-dashboard.png`);

    await page.getByRole("button", { name: "Refresh" }).click();
    const stripDuringRefresh = await page.locator(".scope-strip").innerText({ timeout: 5000 });
    const pagerDuringRefresh = await page.locator(".verification-pager").innerText({ timeout: 5000 });
    expect(stripDuringRefresh).not.toMatch(/\b0\s+(public rollout cells|pilot cells)\b/i);
    expect(pagerDuringRefresh).not.toMatch(/\bof\s+0\+?\s+assets\b/i);
    await expect(page.getByRole("button", { name: "Refresh" })).toBeVisible({ timeout: 60000 });

    const tableRows = page.locator(".verification-table tbody tr");
    await expect(tableRows.first()).toBeVisible({ timeout: 60000 });
    await tableRows.first().click();
    await expect(page.locator(".asset-passport")).toBeVisible({ timeout: 30000 });
    await expect(page.locator(".asset-passport")).toContainText(/Asset passport/i);
    await expect(page.locator(".asset-passport")).toContainText(/Total Potential/i);
    await expect(page.locator(".asset-passport")).toContainText(/Net Annual Increment/i);
    await screenshot(page, testInfo, `${name}-03-asset-passport.png`);

    const focusButton = page.getByRole("button", { name: /^Focus map$/ });
    if ((await focusButton.count()) && (await focusButton.first().isEnabled())) {
      await focusButton.first().click();
      await expect(page.locator(".app-shell.is-map-focus")).toBeVisible();
      await page.getByRole("button", { name: /^Exit focus$/ }).click();
    }

    await page.getByRole("button", { name: /Overview/i }).click();
    await expect(page.getByLabel("Portfolio overview")).toBeVisible({ timeout: 30000 });
    await screenshot(page, testInfo, `${name}-04-overview-return.png`);

    expect(badResponses).toEqual([]);
    expect(consoleErrors).toEqual([]);
    await context.close();
  });
}

async function screenshot(page, testInfo, filename) {
  const targetDir = screenshotDir || testInfo.outputDir;
  fs.mkdirSync(targetDir, { recursive: true });
  await page.waitForTimeout(700);
  await page.screenshot({ path: path.join(targetDir, filename), fullPage: true });
}

function writeAccessProof(testInfo, payload) {
  const targetDir = screenshotDir || testInfo.outputDir;
  fs.mkdirSync(targetDir, { recursive: true });
  fs.writeFileSync(
    path.join(targetDir, "stage6-access-boundary.json"),
    `${JSON.stringify(payload, null, 2)}\n`
  );
}

function sanitizedFrontendUrl() {
  const url = new URL(frontendUrl);
  url.username = "";
  url.password = "";
  return url.toString();
}

function parseBasicAuth(value) {
  if (!value || !value.includes(":")) {
    return null;
  }
  const [username, ...passwordParts] = value.split(":");
  return { username, password: passwordParts.join(":") };
}
