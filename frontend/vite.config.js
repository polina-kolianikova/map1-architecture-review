import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const target = process.env.VITE_API_PROXY_TARGET || "http://localhost:8000";
const parsed = new URL(target);
if (parsed.protocol !== "http:" || !["localhost", "backend"].includes(parsed.hostname) || parsed.username || parsed.password) {
  throw new Error("Use a local showcase API target without credentials.");
}
export default defineConfig({
  plugins: [react()],
  cacheDir: "node_modules/.vite",
  build: { chunkSizeWarningLimit: 2100 },
  server: { host: "localhost", port: 5173, proxy: { "/api": { target, changeOrigin: true, rewrite: (path) => path.replace(/^\/api/, "") } } },
});
