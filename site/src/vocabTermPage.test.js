// @vitest-environment node

import { readFileSync } from "node:fs";

import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";

const routeSource = readFileSync(
  new URL("../../api/routes/vocab.py", import.meta.url),
  "utf8",
);
const copyScriptMatch = routeSource.match(
  /copy_script_html = """\s*<script>([\s\S]*?)<\/script>""" if supports_copy else ""/,
);

if (!copyScriptMatch) {
  throw new Error("Could not locate the vocabulary term-page copy script");
}

const copyScript = copyScriptMatch[1];
const canonicalUri = "https://universalevidence.com/vocab/states/Malaria";

function openTermPage({
  includeCopyControls = true,
  clipboardWrite = async () => {},
} = {}) {
  const controls = includeCopyControls
    ? `<button id="copy-uri" data-uri="${canonicalUri}">Copy URI</button>
       <p id="copy-uri-status" role="status" aria-live="polite"></p>`
    : "";
  const dom = new JSDOM(
    `<code id="canonical-uri">${canonicalUri}</code>${controls}`,
    {
      runScripts: "outside-only",
      url: canonicalUri,
    },
  );
  const clipboardWrites = [];
  if (clipboardWrite !== null) {
    Object.defineProperty(dom.window.navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value) => {
          clipboardWrites.push(value);
          return clipboardWrite(value);
        },
      },
    });
  }
  dom.window.eval(copyScript);
  return { clipboardWrites, dom };
}

async function settleClipboardHandler() {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("server-rendered vocabulary term-page clipboard control", () => {
  it("copies the complete exact canonical URI and announces success", async () => {
    const { clipboardWrites, dom } = openTermPage();

    dom.window.document.querySelector("#copy-uri").click();
    await settleClipboardHandler();

    expect(clipboardWrites).toEqual([canonicalUri]);
    expect(dom.window.document.querySelector("#copy-uri-status").textContent).toBe(
      "URI copied.",
    );
    expect(dom.window.document.querySelector("#canonical-uri").textContent).toBe(
      canonicalUri,
    );
  });

  it("announces rejection while preserving the selectable URI fallback", async () => {
    const { clipboardWrites, dom } = openTermPage({
      clipboardWrite: async () => {
        throw new Error("denied");
      },
    });

    dom.window.document.querySelector("#copy-uri").click();
    await settleClipboardHandler();

    expect(clipboardWrites).toEqual([canonicalUri]);
    expect(dom.window.document.querySelector("#copy-uri-status").textContent).toBe(
      "Could not copy URI. Select and copy it manually.",
    );
    expect(dom.window.document.querySelector("#canonical-uri").textContent).toBe(
      canonicalUri,
    );
  });

  it("fails accessibly when the Clipboard API is unavailable", async () => {
    const { clipboardWrites, dom } = openTermPage({ clipboardWrite: null });

    dom.window.document.querySelector("#copy-uri").click();
    await settleClipboardHandler();

    expect(clipboardWrites).toEqual([]);
    expect(dom.window.document.querySelector("#copy-uri-status").textContent).toBe(
      "Could not copy URI. Select and copy it manually.",
    );
  });

  it("does nothing when server-side type validation omits copy controls", () => {
    const { clipboardWrites, dom } = openTermPage({ includeCopyControls: false });

    expect(dom.window.document.querySelector("#copy-uri")).toBeNull();
    expect(dom.window.document.querySelector("#copy-uri-status")).toBeNull();
    expect(clipboardWrites).toEqual([]);
  });
});
