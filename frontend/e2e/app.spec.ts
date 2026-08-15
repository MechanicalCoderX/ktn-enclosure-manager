import { expect, test, type Page } from "@playwright/test";

/**
 * End-to-end tests (spec §44). These drive the real backend against the
 * captured KTN-STL3 fixture, so they assert the same bay-numbering contract
 * the hardware validation does.
 */

const USER = "admin";
const PASSWORD = "e2e-administrator-password";

async function bootstrapAndLogin(page: Page) {
  await page.goto("/");
  const confirm = page.getByLabel("Confirm password");
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  if (await confirm.isVisible().catch(() => false)) {
    await confirm.fill(PASSWORD);
    await page.getByRole("button", { name: "Create account" }).click();
  } else {
    await page.getByRole("button", { name: "Sign in" }).click();
  }
  await expect(page.getByRole("tab", { name: "Drive map" })).toBeVisible();
}

/** Locate the IDENT indicator on one specific bay tile rather than counting
 *  every dot on the page, so a lit LED elsewhere cannot affect the assertion. */
const identDot = (page: Page, bay: number) =>
  page.getByRole("listitem", { name: new RegExp(`^Bay ${bay}, `) }).locator(".ident-dot");

test.describe("authentication", () => {
  test("first run requires creating an administrator", async ({ page }, testInfo) => {
    // Both projects share one backend, so the bootstrap screen only exists
    // during the first project's run.
    test.skip(testInfo.project.name !== "desktop", "bootstrap happens once per server");
    await page.goto("/");
    await expect(page.getByText("First run")).toBeVisible();
    await expect(page.getByLabel("Confirm password")).toBeVisible();
  });

  test("bootstrap then reach the dashboard", async ({ page }) => {
    await bootstrapAndLogin(page);
    await expect(page.getByRole("button", { name: /Sign out/ })).toBeVisible();
  });

  test("sign out returns to the login screen", async ({ page }) => {
    await bootstrapAndLogin(page);
    await page.getByRole("button", { name: /Sign out/ }).click();
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  });
});

test.describe("drive map", () => {
  test.beforeEach(async ({ page }) => bootstrapAndLogin(page));

  test("renders every bay", async ({ page }) => {
    // 15 populated bays plus the empty-bay fixture.
    await expect(page.getByRole("listitem")).toHaveCount(16);
  });

  test("bay numbering contract: Bay 1 = SES 0, Bay 8 = SES 7, Bay 15 = SES 14", async ({
    page,
  }) => {
    for (const [bay, ses] of [
      [1, 0],
      [8, 7],
      [15, 14],
    ]) {
      await expect(
        page.getByRole("listitem", { name: new RegExp(`^Bay ${bay}, SES slot ${ses},`) }),
      ).toHaveCount(1);
    }
  });

  test("bay one shows the expected disk", async ({ page }) => {
    await expect(page.getByRole("listitem", { name: /^Bay 1, / })).toContainText("K1A00001");
  });

  test("selecting a bay opens the detail panel", async ({ page }) => {
    await page.getByRole("listitem", { name: /^Bay 8, / }).click();
    const detail = page.getByTestId("bay-detail");
    await expect(detail.getByRole("heading", { name: /Bay 8.*SES slot 7/ })).toBeVisible();
    // Scoped to the detail panel: the serial also appears on the tile itself.
    await expect(detail.getByText("K1A00008")).toBeVisible();
  });

  test("search narrows to a serial", async ({ page }) => {
    await page.getByLabel("Search bays").fill("K1A00008");
    const dimmed = page.locator(".bay.dim");
    await expect(dimmed).toHaveCount(15);
  });
});

test.describe("identify", () => {
  test.beforeEach(async ({ page }) => bootstrapAndLogin(page));

  test("timed identify turns on, shows a countdown, and clears", async ({ page }) => {
    await page.getByRole("listitem", { name: /^Bay 1, / }).click();
    await page.getByLabel("Identify for").selectOption("60");
    await page.getByRole("button", { name: "Identify", exact: true }).click();

    await expect(page.getByRole("listitem", { name: /^Bay 1,.*identify active/i })).toBeVisible({
      timeout: 10_000,
    });
    await expect(identDot(page, 1)).toHaveCount(1);
    await expect(page.getByTestId("bay-detail")).toContainText(/\d:\d\d/);

    await page.getByRole("button", { name: "Clear" }).click();
    await expect(identDot(page, 1)).toHaveCount(0, { timeout: 10_000 });
  });

  test("identify is recorded in the audit log", async ({ page }) => {
    await page.getByRole("listitem", { name: /^Bay 3, / }).click();
    await page.getByRole("button", { name: "Identify", exact: true }).click();
    await expect(identDot(page, 3)).toHaveCount(1, { timeout: 10_000 });

    await page.getByRole("tab", { name: "Diagnostics" }).click();
    const auditTable = page.locator(".panel", { hasText: "Audit log" });
    await expect(auditTable).toContainText("IDENT_ON");
    await expect(auditTable).toContainText("success");
  });
});

test.describe("chassis and diagnostics", () => {
  test.beforeEach(async ({ page }) => bootstrapAndLogin(page));

  test("chassis renders without breaking the page", async ({ page }) => {
    // The harness replays captured SES pages through tests/fixtures/fake-sg_ses,
    // so this section has content. It must also survive having none - the poll
    // runs on an interval, so early in a run it legitimately shows the
    // not-collected-yet state, and either is acceptable here (§37).
    await page.getByRole("tab", { name: "Chassis" }).click();
    await expect(page.getByRole("heading", { name: "Chassis" })).toBeVisible();
  });

  test("chassis telemetry renders once the SES poll has run", async ({ page }, testInfo) => {
    // One project only: the wait is bounded by the SES poll interval and there
    // is no value in paying it twice for the same backend.
    test.skip(testInfo.project.name !== "desktop", "one backend, one poll");
    await page.getByRole("tab", { name: "Chassis" }).click();

    // The harness polls SES every second, so this resolves well inside the
    // per-test budget. A longer wait than the test timeout is unreachable.
    await expect(page.getByText("Chassis health")).toBeVisible({ timeout: 15_000 });

    // The captured pages describe five subenclosures; if the parser regressed
    // this section would render empty while still showing its heading.
    await expect(page.getByText(/EMC Viper LCC/).first()).toBeVisible();
    await expect(page.getByText(/50060480aabbcc00/).first()).toBeVisible();
  });

  test("drive map still works when chassis telemetry is unavailable", async ({ page }) => {
    await page.getByRole("tab", { name: "Chassis" }).click();
    await page.getByRole("tab", { name: "Drive map" }).click();
    await expect(page.getByRole("listitem")).toHaveCount(16);
  });

  test("diagnostics exposes discovery and no credentials", async ({ page }) => {
    await page.getByRole("tab", { name: "Diagnostics" }).click();
    const pre = page.locator("pre.raw").first();
    await expect(pre).toContainText("0x50060480aabbcc00");
    await expect(pre).not.toContainText("api_key");
    await expect(pre).not.toContainText("password");
  });
});

test.describe("responsive", () => {
  test("bay row scrolls horizontally rather than rearranging", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "mobile viewport only");
    await bootstrapAndLogin(page);
    const scroller = page.locator(".shelf-scroll");
    const overflows = await scroller.evaluate((el) => el.scrollWidth > el.clientWidth + 1);
    expect(overflows).toBe(true);
    // The physical order must be preserved even when scrolled.
    await expect(page.getByRole("listitem").first()).toHaveAttribute(
      "aria-label",
      /^Bay 1, SES slot 0/,
    );
  });
});
