/**
 * Record the README's Identify demo (docs/images/identify-demo.gif).
 *
 * Same rules as screenshots.mjs: shot against the sanitised fixture, never a
 * live shelf, with the synthetic test slot pruned so the footage shows the
 * real 15-bay layout.
 *
 *   KTN_E2E_SYNTHETIC_SLOTS=0 bash scripts/e2e-server.sh &   # backend on :8421
 *   node frontend/scripts/identify-demo.mjs                   # writes .webm
 *   # then convert (palette pass keeps the file small and the colours clean):
 *   ffmpeg -i /tmp/identify-demo/*.webm -vf \
 *     "fps=10,scale=1100:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse" \
 *     docs/images/identify-demo.gif
 */

import { chromium } from "@playwright/test";

const BASE = process.env.KTN_BASE_URL ?? "http://127.0.0.1:8421";
const OUTDIR = "/tmp/identify-demo";
const USER = "admin";
const PASSWORD = "screenshot-administrator";

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1500, height: 620 },
  recordVideo: { dir: OUTDIR, size: { width: 1500, height: 620 } },
  colorScheme: "dark",
});
const page = await context.newPage();

// First run bootstraps; later runs sign in.
await page.goto(BASE);
const confirm = page.getByLabel("Confirm password");
await page.getByLabel("Username").fill(USER);
await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
if (await confirm.isVisible().catch(() => false)) {
  await confirm.fill(PASSWORD);
  await page.getByRole("button", { name: "Create account" }).click();
} else {
  await page.getByRole("button", { name: "Sign in" }).click();
}
await page.getByRole("tab", { name: "Drive map" }).waitFor();
await page.getByLabel("Theme").selectOption("dark");
await page.waitForTimeout(1800); // first poll lands, tiles settle

// The demo: pick a bay, identify it for 60s, let the countdown breathe,
// then clear it - the full round trip a viewer would perform.
await page.getByRole("listitem", { name: /^Bay 4, / }).click();
await page.waitForTimeout(1200);
await page.getByLabel("Identify for").selectOption("60");
await page.getByRole("button", { name: "Identify", exact: true }).click();
await page
  .getByRole("listitem", { name: /^Bay 4,.*identify active/i })
  .waitFor({ timeout: 10_000 });
await page.waitForTimeout(4500); // pulsing dot + ticking countdown on film
await page.getByRole("button", { name: "Clear" }).click();
await page.waitForTimeout(1800);

await context.close(); // flushes the video
await browser.close();
console.log(`video written under ${OUTDIR}/ - convert with the ffmpeg line above`);
