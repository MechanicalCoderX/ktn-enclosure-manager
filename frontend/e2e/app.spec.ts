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

  test("a failed identify shows the error banner and lights nothing", async ({ page }) => {
    // Sever only the identify POST at the network layer. Bay polling still
    // reaches the real backend, so the no-dot assertions below reflect true
    // enclosure state - not a page frozen on data it can no longer refresh.
    // Bay 5 is used by no other test, so a leaked LED elsewhere (bay 3 stays
    // lit after the audit test) cannot mask a false negative here.
    await page.route("**/api/enclosures/*/slots/*/identify", (route) => route.abort());

    await page.getByRole("listitem", { name: /^Bay 5, / }).click();
    await page.getByRole("button", { name: "Identify", exact: true }).click();

    // The rejection text is fetch's browser-specific wording ("Failed to
    // fetch" on Chromium), so assert the banner itself, not the prose.
    await expect(page.locator("main > .notice.error")).toBeVisible();
    await expect(identDot(page, 5)).toHaveCount(0);
    await expect(page.getByRole("listitem", { name: /^Bay 5,.*identify active/i })).toHaveCount(0);
  });
});

test.describe("freshness", () => {
  // The E2E backend runs with KTN_TRUENAS_URL empty, so `sources.truenas` and
  // `sources.smart` are null with no error: those sources are OFF, not stale.
  // That makes this suite the exact control for the trap in the fix - a summary
  // that folded a never-polled source into "oldest" would age the header to
  // "never" and cry fault on a healthy TrueNAS-less deployment.
  test.beforeEach(async ({ page }) => bootstrapAndLogin(page));

  test("the header dates the page to its oldest source, not its fastest", async ({ page }) => {
    const summary = page.getByTestId("freshness").locator("summary");
    // "as of", not "updated": the old wording claimed the slot poll's time for
    // readings taken up to two minutes earlier.
    await expect(summary).toContainText(/^as of \d{1,2}:\d{2}:\d{2}/);
    // Nothing is failing here, so no degraded badge.
    await expect(summary.locator(".badge.warning")).toHaveCount(0);
  });

  test("expanding names every source and says which are not reporting", async ({ page }) => {
    const detail = page.getByRole("group", { name: "Data freshness by source" });
    // Collapsed by default - this is a status line, not a dashboard.
    await expect(detail).toBeHidden();

    await page.getByTestId("freshness").locator("summary").click();
    await expect(detail).toBeVisible();

    // The enclosure is being read, so it carries a real time AND an age.
    // Asserted separately rather than as one pattern: toLocaleTimeString renders
    // "12:00:03" or "12:00:03 PM" depending on the browser locale, and a test
    // that pins the punctuation of a clock fails for reasons that are not bugs.
    await expect(detail).toContainText("Enclosure bay map");
    await expect(detail).toContainText(/\d{1,2}:\d{2}:\d{2}/);
    await expect(detail).toContainText(/\d+s ago/);

    // TrueNAS is not configured on this harness. It must read as absent rather
    // than as stale, and must not raise a warning.
    await expect(detail).toContainText("TrueNAS pools and vdevs");
    await expect(detail).toContainText("SMART temperatures");
    await expect(detail.getByText("not reporting")).toHaveCount(2);
    await expect(detail).not.toContainText("not refreshing");
  });

  test("each detail block is dated to the clock it actually came from", async ({ page }) => {
    // The bug in one assertion: under a single header stamp, the ZFS block and
    // the enclosure block claimed the same freshness. Here they cannot - the
    // enclosure is live and TrueNAS is switched off, and the panel says so
    // separately for each.
    await page.getByRole("listitem", { name: /^Bay 8, / }).click();
    const detail = page.getByTestId("bay-detail");

    await expect(detail.getByTestId("section-physical")).toContainText(
      /as of \d{1,2}:\d{2}:\d{2}/,
    );
    await expect(detail.getByTestId("section-zfs")).toContainText("not reporting");
    await expect(detail.getByTestId("section-smart")).toContainText("not reporting");
    // Disk identity is on no cache at all; it must not borrow a stamp.
    await expect(detail.getByTestId("section-disk")).toContainText("read live");
  });

  test("a healthy page makes no slot-cache failure claim either way", async ({ page }) => {
    // Control for the banner test below: the condition must be absent when the
    // slot poll is fine, or "surfaces a slot-cache failure" would pass trivially.
    await expect(page.getByTestId("slots-error")).toHaveCount(0);
    await expect(page.locator("main")).not.toContainText("not refreshing");
  });

  test("a failing slot cache retracts the accuracy claim rather than repeating it", async ({
    page,
  }) => {
    // The slot cache cannot be made to fail from outside the process - a failed
    // poll keeps serving last-good rows, which is the whole difficulty - so the
    // failure is injected into the response the browser actually receives. Only
    // `sources` is rewritten; the bay rows stay exactly as the backend composed
    // them, which is precisely the situation being tested: the map still looks
    // completely normal.
    await page.route("**/api/enclosures/*/bays", async (route) => {
      const body = await (await route.fetch()).json();
      body.sources.slots_error = "[Errno 19] No such device";
      body.sources.truenas_error = "connection refused";
      await route.fulfill({ json: body });
    });

    const banner = page.getByTestId("slots-error");
    await expect(banner).toBeVisible({ timeout: 10_000 });
    await expect(banner).toContainText("last good snapshot");
    await expect(banner).toContainText("not refreshing");

    // The regression this exists for: the TrueNAS banner used to assert
    // unconditionally that bay state "remains accurate", which is false in
    // exactly this state - the one state where a reader leans on it.
    const main = page.locator("main");
    await expect(main).toContainText("TrueNAS unavailable");
    await expect(main).not.toContainText("remains accurate");
    await expect(main).not.toContainText("still refreshing");

    // And the header stops claiming the page is current.
    await expect(
      page.getByTestId("freshness").locator("summary .badge.warning"),
    ).toBeVisible();
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

  test("cooling shows the step the firmware chose, and offers no way to set it", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "one backend, one poll");
    await page.getByRole("tab", { name: "Chassis" }).click();
    await expect(page.getByText("Chassis health")).toBeVisible({ timeout: 15_000 });

    const cooling = page.getByTestId("chassis-cooling");
    // The captured KTN-STL3 pages carry four non-overall Cooling elements, all
    // at 5300 rpm / code 7 / "Fan at highest speed".
    await expect(cooling.getByText("Fan at highest speed")).toHaveCount(4);
    await expect(cooling.getByText("7 of 7", { exact: true })).toHaveCount(4);
    await expect(cooling.getByText("5300 rpm", { exact: true })).toHaveCount(4);

    // Three-state RQSTED ON, which is the whole reason it is not a checkbox:
    // in this capture one element reports the bit set and three print no such
    // field at all. Rendering absence as "no" would invent a reading (§13).
    await expect(cooling.getByRole("cell", { name: "yes", exact: true })).toHaveCount(1);
    await expect(cooling.getByText("not reported")).toHaveCount(3);

    // All four agree here, so the divergence line must stay silent - otherwise
    // it would be decoration rather than news.
    await expect(cooling).not.toContainText("different speed steps");

    // Nothing on this panel may look actionable: there is no fan control in the
    // application and none is planned (§15).
    await expect(cooling).toContainText("never writes fan speed");
    await expect(cooling.locator("button, select, input")).toHaveCount(0);
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

test.describe("change password", () => {
  // Deliberately the last block in the file: it rotates the one credential
  // every other test's bootstrapAndLogin() depends on, and the backend is
  // shared by both projects (workers: 1, one webServer per run). The test
  // restores the password through the same dialog it exercises; the afterEach
  // below is the net for a failure that lands between change and change-back.
  const ROTATED = "e2e-rotated-password";

  test.afterEach(async ({ request }) => {
    // A test that died holding the rotated credential would fail every later
    // login in the run with a misleading 401. The request fixture keeps its
    // own cookie jar, so the login here establishes the session the change
    // needs. Every auth POST must carry the CSRF header - the server refuses
    // header-less mutations before it ever reads the body.
    const csrf = { "X-KTN-Request": "1" };
    const asSuite = await request.post("/api/auth/login", {
      headers: csrf,
      data: { username: USER, password: PASSWORD },
    });
    if (asSuite.ok()) return; // the credential the suite expects still works
    const asRotated = await request.post("/api/auth/login", {
      headers: csrf,
      data: { username: USER, password: ROTATED },
    });
    expect(asRotated.ok(), "admin password is neither the suite's nor the rotated one").toBe(true);
    const restored = await request.post("/api/auth/password", {
      headers: csrf,
      data: { current_password: ROTATED, new_password: PASSWORD },
    });
    expect(restored.ok()).toBe(true);
  });

  test("refuses a mismatch client-side, then a real change signs the session out", async ({
    page,
  }) => {
    await bootstrapAndLogin(page);
    await page.getByRole("button", { name: "Change password" }).click();
    const dialog = page.getByRole("dialog");

    // Both new-password fields satisfy the native minLength=12 constraint, so
    // submission reaches the dialog's handler and the refusal asserted is its
    // own mismatch check - not the browser's validation bubble, which proves
    // nothing about this code.
    await dialog.getByLabel("Current password").fill(PASSWORD);
    await dialog.getByLabel("New password", { exact: true }).fill(ROTATED);
    await dialog.getByLabel("Confirm new password").fill(`${ROTATED}-typo`);
    await dialog.getByRole("button", { name: "Change password" }).click();
    await expect(dialog.getByText("New passwords do not match")).toBeVisible();
    // The refusal happens before any request leaves the page, so the session
    // must still be live behind the dialog.
    await expect(page.getByRole("tab", { name: "Drive map" })).toBeVisible();

    // Fix the confirmation and submit for real. The change bumps the session
    // epoch, which invalidates this session's cookie too, so the app must
    // land on the login screen with the notice - not a dead dashboard
    // answering 401 on its next poll.
    await dialog.getByLabel("Confirm new password").fill(ROTATED);
    await dialog.getByRole("button", { name: "Change password" }).click();
    await expect(
      page.getByText("Password changed. Sign in with your new password."),
    ).toBeVisible({ timeout: 10_000 });

    // The rotated credential works where the old cookie no longer does.
    await page.getByLabel("Username").fill(USER);
    await page.getByLabel("Password", { exact: true }).fill(ROTATED);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("tab", { name: "Drive map" })).toBeVisible();

    // Rotate back through the same UI so the rest of the run - including the
    // other project's pass over this file - signs in with the suite password.
    await page.getByRole("button", { name: "Change password" }).click();
    await dialog.getByLabel("Current password").fill(ROTATED);
    await dialog.getByLabel("New password", { exact: true }).fill(PASSWORD);
    await dialog.getByLabel("Confirm new password").fill(PASSWORD);
    await dialog.getByRole("button", { name: "Change password" }).click();
    await expect(
      page.getByText("Password changed. Sign in with your new password."),
    ).toBeVisible({ timeout: 10_000 });
    await page.getByLabel("Username").fill(USER);
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("tab", { name: "Drive map" })).toBeVisible();
  });
});
