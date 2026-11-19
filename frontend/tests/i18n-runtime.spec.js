import { expect, test } from "@playwright/test";

const frontendUrl = process.env.FRONTEND_URL || "http://127.0.0.1:5173";
const auth = parseBasicAuth(process.env.FRONTEND_AUTH || "");

if (auth) {
  test.use({ httpCredentials: auth });
}

test("runtime locale hook applies the resolved document language", async ({ page }) => {
  await page.addInitScript(() => {
    window.__MAP1_CONFIG__ = {
      availableLocales: ["en"],
      locale: "en-US",
    };
  });
  await page.goto(frontendUrl, { waitUntil: "domcontentloaded" });

  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.getByRole("heading", { name: "Map 1 Turkey" })).toBeVisible();
  await expect(page.getByText("Forest asset command center")).toBeVisible();
});

function parseBasicAuth(value) {
  if (!value || !value.includes(":")) {
    return null;
  }
  const [username, ...rest] = value.split(":");
  return { username, password: rest.join(":") };
}
