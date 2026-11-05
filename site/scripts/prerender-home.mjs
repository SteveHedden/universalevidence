import { readFile, writeFile, rename } from "node:fs/promises";
import { createServer } from "vite";

// Render the same initial UI used by the browser, with no API calls or effects.
const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
try {
  const { renderHome } = await server.ssrLoadModule("/src/prerender.tsx");
  const path = new URL("../dist/index.html", import.meta.url);
  const html = await readFile(path, "utf8");
  const placeholder = '<div id="root"></div>';
  if (!html.includes(placeholder)) throw new Error("Missing homepage render target");
  await writeFile(path, html.replace(placeholder, () => `<div id="root">${renderHome()}</div>`));
  await rename(path, new URL("../dist/explore.html", import.meta.url));
} finally {
  await server.close();
}
