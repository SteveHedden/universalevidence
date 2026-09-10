// @vitest-environment node

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const stylesPath = new URL("./styles.css", import.meta.url);
const styles = readFileSync(stylesPath, "utf8");

describe("responsive Search controls", () => {
  it("does not hide the hierarchy Browse control at mobile widths", () => {
    expect(styles).not.toMatch(/\.browse-btn\s*\{[^}]*display\s*:\s*none/);
  });

  it("does not reserve desktop drawer space when Browse is open on mobile", () => {
    const mediaStart = styles.indexOf("@media (max-width: 720px)");
    const mediaEnd = styles.indexOf("/* ── multi-chip axis selector", mediaStart);
    const mobileStyles = styles.slice(mediaStart, mediaEnd);

    expect(mobileStyles).toMatch(
      /body\.browse-open \.app-shell\s*\{[^}]*padding-right\s*:\s*16px/,
    );
  });
});
