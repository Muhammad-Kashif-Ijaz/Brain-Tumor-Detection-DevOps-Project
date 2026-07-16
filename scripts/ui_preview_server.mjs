import { createReadStream, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(fileURLToPath(new URL("..", import.meta.url)));
const port = Number(process.env.PORT || 5080);
const mimeTypes = {
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".svg": "image/svg+xml",
};

function renderTemplate() {
  return readFileSync(join(projectRoot, "templates", "index.html"), "utf8")
    .replace(
      /\{\{\s*url_for\('static',\s*filename='css\/styles\.css'\)\s*\}\}\?v=\{\{\s*asset_version\s*\}\}/g,
      "/static/css/styles.css",
    )
    .replace(
      /\{\{\s*url_for\('static',\s*filename='js\/app\.js'\)\s*\}\}\?v=\{\{\s*asset_version\s*\}\}/g,
      "/static/js/app.js",
    );
}

function sendJson(response, status, payload) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(payload));
}

const server = createServer((request, response) => {
  const url = new URL(request.url || "/", `http://127.0.0.1:${port}`);

  if (request.method === "GET" && url.pathname === "/") {
    response.writeHead(200, {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    });
    response.end(renderTemplate());
    return;
  }

  if (request.method === "GET" && url.pathname.startsWith("/static/")) {
    const relativePath = normalize(url.pathname.slice(1));
    const filePath = resolve(projectRoot, relativePath);
    const staticRoot = resolve(projectRoot, "static");
    if (!filePath.startsWith(staticRoot) || !statSafe(filePath)) {
      response.writeHead(404);
      response.end("Not found");
      return;
    }
    response.writeHead(200, {
      "Content-Type": mimeTypes[extname(filePath).toLowerCase()] || "application/octet-stream",
      "Cache-Control": "no-store",
    });
    createReadStream(filePath).pipe(response);
    return;
  }

  if (url.pathname.startsWith("/api/")) {
    sendJson(response, 503, {
      status: "preview-only",
      message: "The local interface preview is running. Model inference runs in the Docker deployment.",
    });
    return;
  }

  response.writeHead(404);
  response.end("Not found");
});

function statSafe(path) {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

server.listen(port, "127.0.0.1", () => {
  process.stdout.write(`CerebraVue preview: http://127.0.0.1:${port}\n`);
});
