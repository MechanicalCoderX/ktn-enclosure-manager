/**
 * Regenerate the README screenshots.
 *
 * Deliberately shot against the captured KTN-STL3 *fixture*, not a live shelf:
 * the fixture is already sanitised (serials like K1A0000N, pool `tank`), so the
 * images cannot leak real drive identifiers into a public repository. Shooting
 * a production system would.
 *
 *   bash scripts/e2e-server.sh &          # backend on :8421 against fixtures
 *   node frontend/scripts/screenshots.mjs
 *
 * Sizes match what was there before so the README layout does not shift.
 */

import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";

const BASE = process.env.KTN_BASE_URL ?? "http://127.0.0.1:8421";
const OUT = new URL("../../docs/images/", import.meta.url).pathname;
const USER = "admin";
const PASSWORD = "screenshot-administrator";

const WIDTH = 1500;
const HEIGHT = 940;

async function signIn(page) {
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
  // Let the first poll land so tiles are populated rather than mid-render.
  await page.waitForTimeout(1500);
}

async function setTheme(page, theme) {
  await page.getByLabel("Theme").selectOption(theme);
  await page.waitForTimeout(400);
}

async function shot(page, name, opts = {}) {
  const path = `${OUT}${name}`;
  await page.screenshot({ path, ...opts });
  console.log(`  wrote ${name}`);
}

const browser = await chromium.launch();
await mkdir(OUT, { recursive: true });

// ---------------------------------------------------------------- desktop
const page = await browser.newPage({
  viewport: { width: WIDTH, height: HEIGHT },
  deviceScaleFactor: 1,
  colorScheme: "dark",
});
await signIn(page);

await setTheme(page, "dark");
await shot(page, "drive-map-dark.png");

await setTheme(page, "light");
await shot(page, "drive-map-light.png");

await setTheme(page, "dark");

// Bay detail: pick a populated bay so the panel has real content.
await page.getByRole("listitem", { name: /^Bay 6, / }).click();
await page.waitForTimeout(600);
await shot(page, "bay-detail.png");

await page.getByRole("tab", { name: "Chassis" }).click();
// Wait for real telemetry rather than a fixed delay: the SES poll runs on a
// 30s interval, and a short wait catches the "not collected yet" state - which
// is how a 27KB screenshot of an error message once replaced a good one.
await page.getByText("Chassis health").waitFor({ timeout: 60_000 });
await page.waitForTimeout(800);
await shot(page, "chassis.png", { fullPage: true });

await page.getByRole("tab", { name: "Diagnostics" }).click();
await page.waitForTimeout(1200);
await shot(page, "diagnostics.png");

await page.close();

// ----------------------------------------------------------------- mobile
const mobile = await browser.newPage({
  viewport: { width: 412, height: 915 },
  deviceScaleFactor: 1,
  isMobile: true,
  hasTouch: true,
  colorScheme: "dark",
});
await signIn(mobile);
await setTheme(mobile, "dark");
await shot(mobile, "mobile.png");
await mobile.close();

await browser.close();
console.log("done");
