import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";
import path from "node:path";

const frontend = fileURLToPath(new URL("../../", import.meta.url));
export const artifacts = process.env.MAP1_BROWSER_ARTIFACTS || path.resolve(
  frontend, "../../map1-turkey-private/test-results/click-passport"
);

export default defineConfig({
  testDir: fileURLToPath(new URL("./", import.meta.url)),
  testMatch: "*.spec.js",
  outputDir: path.join(artifacts, "results"),
  reporter: [["line"], ["json", { outputFile: path.join(artifacts, "report.json") }]],
  workers: 1,
  retries: 0,
  timeout: 30000,
  expect: { timeout: 7000 },
  use: {
    baseURL: "http://127.0.0.1:5187",
    browserName: "chromium",
    viewport: { width: 1440, height: 1100 },
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: {
      args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--disable-dev-shm-usage"],
    },
  },
  webServer: {
    command: "node node_modules/vite/bin/vite.js --config tests/click-passport/vite.config.mjs --configLoader runner --host 127.0.0.1 --port 5187 --strictPort",
    cwd: frontend,
    url: "http://127.0.0.1:5187",
    reuseExistingServer: false,
    env: { VITE_CACHE_DIR: path.join(artifacts, "vite-cache") },
  },
});
