import { defineConfig, mergeConfig } from "vite";
import appConfig from "../../vite.config.js";

// Separate local server; no app/config edits and no backend proxy in this harness.
const config = mergeConfig(appConfig, defineConfig({ server: { hmr: false } }));
config.server.proxy = undefined;
export default config;
