import http from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const directory = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(directory, process.env.SHOWCASE_DIST_DIR || "dist");
const api = new URL(process.env.API_PROXY_TARGET || process.env.VITE_PREVIEW_API_PROXY_TARGET || "http://localhost:8000");
if (api.protocol !== "http:" || !["localhost", "backend"].includes(api.hostname) || api.username || api.password) {
  throw new Error("Use a local showcase API target without credentials.");
}
const host = process.env.SHOWCASE_BIND_HOST || "localhost";
if (!["localhost", "0.0.0.0"].includes(host)) throw new Error("Unsupported local bind host.");
const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json" };
const server = http.createServer(async (req, res) => {
  const pathname = new URL(req.url || "/", "http://localhost").pathname;
  if (pathname.startsWith("/api/")) {
    const target = new URL(api);
    const requestUrl = new URL(req.url || "/", "http://localhost");
    target.pathname = requestUrl.pathname.slice(4);
    target.search = requestUrl.search;
    const upstream = http.request(target, { method: req.method, headers: { "content-type": req.headers["content-type"] || "application/json" } }, (response) => {
      res.writeHead(response.statusCode || 502, { "content-type": response.headers["content-type"] || "application/json" });
      response.pipe(res);
    });
    upstream.on("error", () => { res.writeHead(502, { "content-type": "application/json" }); res.end(JSON.stringify({ detail: "Start the local showcase API." })); });
    req.pipe(upstream);
    return;
  }
  if (pathname === "/runtime-config.js") {
    res.writeHead(200, { "content-type": "text/javascript", "cache-control": "no-store" });
    res.end("window.__MAP1_CONFIG__ = {};\n");
    return;
  }
  try {
    const relative = pathname === "/" ? "index.html" : decodeURIComponent(pathname).replace(/^\/+/, "");
    const file = path.resolve(root, relative);
    if (!file.startsWith(root + path.sep)) { res.writeHead(403); res.end(); return; }
    const body = await readFile(file);
    res.writeHead(200, { "content-type": types[path.extname(file)] || "application/octet-stream" });
    res.end(body);
  } catch { res.writeHead(404); res.end("Not found"); }
});
server.listen(Number(process.env.PORT || 5173), host);
