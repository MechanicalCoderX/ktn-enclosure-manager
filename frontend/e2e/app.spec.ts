import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

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
  page.getByRole("button", { name: new RegExp(`^Bay ${bay}\\b`) }).locator(".ident-dot");

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
        page.getByRole("button", { name: new RegExp(`^Bay ${bay}\\b.*SES slot ${ses}\\b`) }),
      ).toHaveCount(1);
    }
  });

  test("bay one shows the expected disk", async ({ page }) => {
    await expect(page.getByRole("button", { name: /^Bay 1\b/ })).toContainText("K1A00001");
  });

  test("selecting a bay opens the detail panel", async ({ page }) => {
    await page.getByRole("button", { name: /^Bay 8\b/ }).click();
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
    await page.getByRole("button", { name: /^Bay 1\b/ }).click();
    await page.getByLabel("Identify for").selectOption("60");
    await page.getByRole("button", { name: "Identify", exact: true }).click();

    await expect(page.getByRole("button", { name: /^Bay 1\b.*identify active/i })).toBeVisible({
      timeout: 10_000,
    });
    await expect(identDot(page, 1)).toHaveCount(1);
    await expect(page.getByTestId("bay-detail")).toContainText(/\d:\d\d/);

    await page.getByRole("button", { name: "Clear" }).click();
    await expect(identDot(page, 1)).toHaveCount(0, { timeout: 10_000 });
  });

  test("identify is recorded in the audit log", async ({ page }) => {
    await page.getByRole("button", { name: /^Bay 3\b/ }).click();
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

    await page.getByRole("button", { name: /^Bay 5\b/ }).click();
    await page.getByRole("button", { name: "Identify", exact: true }).click();

    // The rejection text is fetch's browser-specific wording ("Failed to
    // fetch" on Chromium), so assert the banner itself, not the prose.
    await expect(page.locator("main > .notice.error")).toBeVisible();
    await expect(identDot(page, 5)).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^Bay 5\b.*identify active/i })).toHaveCount(0);
  });
});

test.describe("identify expiry", () => {
  // Reported 2026-10-29: after a 30 s Identify on Bay 15 the map's marker kept
  // blinking for about two seconds after the LED went out, because the map only
  // learned of the server's clear on the next five-second bay poll.
  //
  // The browser's clock is faked (page.clock) and /bays is rewritten so that
  // Bay 15's IDENT, the server's clock and the server's clear are all under the
  // test's control. Nothing here waits in real time for a poll; time moves only
  // when the test moves it, and it is paused in between.
  const BAY = 15;
  const SLOT = 14;
  const POLL_MS = 5000; // POLL_BAYS_MS in App.tsx
  const STEP_MS = 100;
  // The audit log of the report: system:timer IDENT_OFF landed 0.59 s after
  // the expiry (a one-second timer tick plus the write). The fake server keeps
  // reporting the bay lit for that long after expiry, as the real one does.
  const SERVER_CLEAR_LAG_MS = 600;
  const SENT_AT_HEADER = "x-e2e-sent-at";

  type FakeServer = {
    /** The paused browser clock, as last advanced by the test. */
    browserNow: number;
    /** Server clock minus browser clock. */
    skewMs: number;
    lit: boolean;
    /** Bay 15's expiry on the SERVER's clock; null = "until cleared". */
    expiresAt: number | null;
    /** Browser time at which each /bays request was sent. */
    requests: number[];
  };

  async function fakeServer(page: Page, skewMs: number): Promise<FakeServer> {
    const fake: FakeServer = { browserNow: 0, skewMs, lit: false, expiresAt: null, requests: [] };
    let template: { bays: Record<string, unknown>[] } | null = null;
    await page.route("**/api/enclosures/*/bays", async (route) => {
      // Stamped by the page itself (see beforeEach), so it is the instant the
      // request left, not whenever this handler happened to run.
      const sentAt = Number(route.request().headers()[SENT_AT_HEADER]);
      fake.requests.push(sentAt);
      // One real response for the shape; after that answer at once, so the
      // response lands while the page clock is still paused at sentAt.
      template ??= await (await route.fetch()).json();
      const body = structuredClone(template!) as Record<string, unknown> & {
        bays: Record<string, unknown>[];
      };
      const serverNow = sentAt + fake.skewMs;
      const lit =
        fake.lit &&
        (fake.expiresAt === null || serverNow < fake.expiresAt + SERVER_CLEAR_LAG_MS);
      for (const bay of body.bays) {
        // Other bays are blanked so a LED left lit by an earlier test cannot
        // schedule a timer of its own and add requests to the count.
        const mine = bay.ses_slot === SLOT && lit;
        bay.locate = mine;
        bay.ident_origin = mine ? "app" : null;
        bay.ident_expires_at =
          mine && fake.expiresAt !== null ? new Date(fake.expiresAt).toISOString() : null;
      }
      body.generated_at = new Date(serverNow).toISOString();
      await route.fulfill({ json: body });
    });
    return fake;
  }

  /** Move the paused browser clock to `target` in small steps, letting each
   *  step's responses land before the next, so a response is received at the
   *  browser time the fake server stamped it with. */
  async function advanceTo(page: Page, fake: FakeServer, target: number, step = STEP_MS) {
    while (fake.browserNow < target) {
      const next = Math.min(fake.browserNow + step, target);
      const delta = next - fake.browserNow;
      fake.browserNow = next;
      await page.clock.runFor(delta);
      await page.waitForTimeout(10); // real time; the page clock stays paused
    }
  }

  const requestsIn = (fake: FakeServer, from: number, to: number) =>
    fake.requests.filter((t) => t > from && t <= to).length;

  /** Pause the clock, learn the poll's phase, and return a deadline that sits
   *  midway between two polls - so no poll can land near it and any request
   *  seen there is the expiry refetch and nothing else. */
  async function start(page: Page, fake: FakeServer): Promise<number> {
    const t0 = Math.ceil((Date.now() + 60_000) / 1000) * 1000;
    fake.browserNow = t0;
    await page.clock.pauseAt(t0);
    await advanceTo(page, fake, t0 + POLL_MS);
    // Exactly one interval tick falls in any one-interval window.
    const ticks = fake.requests.filter((t) => t > t0);
    expect(ticks, "expected exactly one bay poll in one poll interval").toHaveLength(1);
    return ticks[0] + 3 * POLL_MS + POLL_MS / 2;
  }

  test.beforeEach(async ({ page }) => {
    // Every request the page makes carries its own (fake) send time. Test
    // instrumentation only: the app neither sends nor reads this header.
    await page.addInitScript((header) => {
      const realFetch = window.fetch.bind(window);
      window.fetch = (input, init) => {
        const headers = new Headers(init?.headers);
        headers.set(header, String(Date.now()));
        return realFetch(input, { ...init, headers });
      };
    }, SENT_AT_HEADER);
    await page.clock.install();
    await bootstrapAndLogin(page);
  });

  // Both directions of skew, 45 s each way. With the server's clock AHEAD, a
  // fix that compared ident_expires_at against the browser's clock would leave
  // the marker lit 45 s past expiry; with it BEHIND, it would put the marker
  // out 45 s early - in fact on arrival.
  for (const skewMs of [45_000, -45_000]) {
    test(`the marker goes out at expiry, without waiting for a poll (server clock ${
      skewMs > 0 ? "ahead" : "behind"
    })`, async ({ page }) => {
      const fake = await fakeServer(page, skewMs);
      const deadline = await start(page, fake);
      fake.lit = true;
      fake.expiresAt = deadline + skewMs;

      await advanceTo(page, fake, deadline - 500);
      await expect(identDot(page, BAY)).toHaveCount(1);

      // At expiry: dark, while the server still reports the bay lit (it clears
      // SERVER_CLEAR_LAG_MS later) and the next poll is still 2.5 s away.
      await advanceTo(page, fake, deadline + 300);
      await expect(identDot(page, BAY)).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: new RegExp(`^Bay ${BAY}\\b.*identify active`, "i") }),
      ).toHaveCount(0);
      // Exactly one refetch, fired at the expiry itself ...
      expect(requestsIn(fake, deadline - STEP_MS, deadline + 300)).toBe(1);

      // ... and not a loop: it came back still lit with a lapsed expiry, which
      // must neither relight the marker nor arm another refetch.
      await advanceTo(page, fake, deadline + 2000);
      expect(requestsIn(fake, deadline + 300, deadline + 2000)).toBe(0);
      await expect(identDot(page, BAY)).toHaveCount(0);

      // The next ordinary poll carries the server's clear; still dark.
      await advanceTo(page, fake, deadline + 3000);
      await expect(identDot(page, BAY)).toHaveCount(0);
    });
  }

  test("a later expiry from the server cancels the pending local stop", async ({ page }) => {
    const skewMs = 45_000;
    const fake = await fakeServer(page, skewMs);
    const deadline = await start(page, fake);
    fake.lit = true;
    fake.expiresAt = deadline + skewMs;
    await advanceTo(page, fake, deadline - 2 * POLL_MS);
    await expect(identDot(page, BAY)).toHaveCount(1);

    // Identify is re-triggered: the server's expiry moves ten seconds later,
    // and the poll 2.5 s before the old deadline carries it.
    const extended = deadline + 2 * POLL_MS;
    fake.expiresAt = extended + skewMs;

    await advanceTo(page, fake, deadline + 1000);
    await expect(identDot(page, BAY)).toHaveCount(1);
    expect(requestsIn(fake, deadline - STEP_MS, deadline + 1000), "stale timer fired").toBe(0);

    await advanceTo(page, fake, extended - 500);
    await expect(identDot(page, BAY)).toHaveCount(1);
    await advanceTo(page, fake, extended + 300);
    await expect(identDot(page, BAY)).toHaveCount(0);
    expect(requestsIn(fake, extended - STEP_MS, extended + 300)).toBe(1);
  });

  test("'until cleared' has no expiry and keeps blinking", async ({ page }) => {
    const fake = await fakeServer(page, 45_000);
    await start(page, fake);
    fake.lit = true;
    fake.expiresAt = null;
    await advanceTo(page, fake, fake.browserNow + POLL_MS);
    await expect(identDot(page, BAY)).toHaveCount(1);

    // Past the longest timed duration (5 min) and then some, in poll-sized
    // steps: still lit, and only ordinary polls were made.
    const before = fake.requests.length;
    await advanceTo(page, fake, fake.browserNow + 6 * 60_000, POLL_MS);
    await expect(identDot(page, BAY)).toHaveCount(1);
    expect(fake.requests.length - before).toBeLessThanOrEqual((6 * 60_000) / POLL_MS);
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
    await page.getByRole("button", { name: /^Bay 8\b/ }).click();
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
    await expect(page.getByRole("listitem").first().getByRole("button")).toHaveAccessibleName(
      /^Bay 1\b.*SES slot 0\b/,
    );
  });
});

/**
 * WCAG 2.x contrast of every visible text element under `selector`, measured
 * against what is actually painted behind it: the ancestor chain's backgrounds
 * are composited top-down (alpha and group opacity included), so a transparent
 * element is judged against the first painted surface beneath it rather than
 * against nothing. Disabled controls and search-dimmed tiles are skipped (WCAG
 * exempts inactive components; dimming is the deliberate "not a match" cue).
 * Form controls own no text node, so they are judged on what they display: a
 * <select> on its own colour (the chosen option's label is drawn in it), an
 * input on its typed text when it has a value, else on its ::placeholder.
 * Text is judged on its `color` and, where it differs, on the
 * -webkit-text-fill-color the glyphs are actually painted with: browsers
 * paint with the fill, so an override there is invisible to `color`
 * alone. A ::placeholder's own opacity is composited onto its text colour,
 * because it dims the placeholder without dimming the field behind it.
 * Returns how many elements were measured, how many of those were form
 * controls showing a value, every judged placeholder's computed opacity, and
 * those below 4.5:1 (3:1 large).
 */
async function lowContrast(page: Page, selector: string) {
  return page.evaluate((sel) => {
    type C = { r: number; g: number; b: number; a: number };
    const parse = (s: string): C => {
      const p = (s.match(/rgba?\(([^)]+)\)/)?.[1] ?? "0 0 0 0")
        .split(/[ ,/]+/).filter(Boolean).map(Number);
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    };
    const over = (t: C, u: C): C => ({
      r: t.r * t.a + u.r * (1 - t.a),
      g: t.g * t.a + u.g * (1 - t.a),
      b: t.b * t.a + u.b * (1 - t.a),
      a: 1,
    });
    const lum = (c: C) => {
      const f = (v: number) =>
        (v /= 255) <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
      return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
    };
    // The pixel under `el`, with its text colour on top when `fg` is given.
    const paint = (el: Element, fg: C | null): C => {
      const chain: Element[] = [];
      for (let n: Element | null = el; n; n = n.parentElement) chain.unshift(n);
      const walk = (i: number, px: C): C => {
        if (i === chain.length) return fg ? over(fg, px) : px;
        const cs = getComputedStyle(chain[i]);
        const behind = px;
        px = walk(i + 1, over(parse(cs.backgroundColor), px));
        const op = Number(cs.opacity);
        return op < 1 ? over({ ...px, a: op }, behind) : px;
      };
      return walk(0, { r: 255, g: 255, b: 255, a: 1 });
    };
    const hex = (c: C) =>
      "#" + [c.r, c.g, c.b].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
    const low: { label: string; ratio: number; detail: string }[] = [];
    let checked = 0;
    let controls = 0;
    const placeholderOpacity: string[] = [];
    // `ink` is the computed style the text takes its colours from: the element
    // itself, or its ::placeholder. `alpha` is that pseudo-element's opacity.
    const judge = (el: Element, label: string, ink: CSSStyleDeclaration, alpha = 1) => {
      const cs = getComputedStyle(el);
      const bg = paint(el, null);
      const size = parseFloat(cs.fontSize);
      const large = size >= 24 || (size >= 18.66 && Number(cs.fontWeight) >= 700);
      const fill = ink.getPropertyValue("-webkit-text-fill-color") || ink.color;
      checked++;
      for (const [color, which] of fill === ink.color
        ? [[ink.color, ""]]
        : [[ink.color, " (color)"], [fill, " (text-fill)"]]) {
        const c = parse(color);
        const text = paint(el, { ...c, a: c.a * alpha });
        const [hi, lo] = [lum(text), lum(bg)].sort((a, b) => b - a);
        const ratio = (hi + 0.05) / (lo + 0.05);
        if (ratio < (large ? 3 : 4.5))
          low.push({
            label: label + which,
            ratio,
            detail: `${label}${which}: ${hex(text)} on ${hex(bg)} = ${ratio.toFixed(2)}:1 at ${size}px`,
          });
      }
    };
    for (const el of document.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0 || el.closest(":disabled, .dim, option")) continue;
      if (el instanceof HTMLInputElement) {
        if (el.value) {
          controls++;
          judge(el, `input[type=${el.type}] value`, getComputedStyle(el));
        } else if (el.placeholder) {
          const ph = getComputedStyle(el, "::placeholder");
          placeholderOpacity.push(ph.opacity);
          judge(el, "::placeholder", ph, Number(ph.opacity));
        }
        continue;
      }
      if (el instanceof HTMLSelectElement) {
        controls++;
        judge(el, `select "${el.selectedOptions[0]?.text.slice(0, 24) ?? ""}"`,
              getComputedStyle(el));
        continue;
      }
      if ([...el.childNodes].some((n) => n.nodeType === 3 && n.textContent?.trim()))
        judge(el, `${el.className || el.tagName.toLowerCase()} "${el.textContent?.trim().slice(0, 24)}"`,
              getComputedStyle(el));
    }
    return { checked, controls, placeholderOpacity, low };
  }, selector);
}

/**
 * WCAG 1.4.11 contrast of `target`'s keyboard focus ring: the outline colour
 * (with the opacity of the element and its ancestors, which dims an outline
 * too) against the surface it is drawn on, which is whatever is painted behind
 * the element's parent - the ring sits outside the element's own box. The
 * element is reached by keyboard, so :focus-visible applies.
 * Returns the ratio, or 0 when no outline is drawn.
 */
async function focusRing(page: Page, target: Locator) {
  // Focus it, then leave and come back by keyboard: a ring is only promised
  // for keyboard focus, and a mouse click earlier in the test would otherwise
  // leave :focus-visible unset.
  await target.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(target).toBeFocused();
  return target.evaluate((el) => {
    type C = { r: number; g: number; b: number; a: number };
    const parse = (s: string): C => {
      const p = (s.match(/rgba?\(([^)]+)\)/)?.[1] ?? "0 0 0 0")
        .split(/[ ,/]+/).filter(Boolean).map(Number);
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    };
    const over = (t: C, u: C): C => ({
      r: t.r * t.a + u.r * (1 - t.a),
      g: t.g * t.a + u.g * (1 - t.a),
      b: t.b * t.a + u.b * (1 - t.a),
      a: 1,
    });
    const lum = (c: C) => {
      const f = (v: number) =>
        (v /= 255) <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
      return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
    };
    if (!el.matches(":focus-visible")) return 0;
    const cs = getComputedStyle(el);
    if (cs.outlineStyle === "none" || parseFloat(cs.outlineWidth) < 2) return 0;
    let surface: C = { r: 255, g: 255, b: 255, a: 1 };
    const chain: Element[] = [];
    for (let n = el.parentElement; n; n = n.parentElement) chain.unshift(n);
    for (const n of chain) surface = over(parse(getComputedStyle(n).backgroundColor), surface);
    let alpha = 1;
    for (let n: Element | null = el; n; n = n.parentElement) alpha *= Number(getComputedStyle(n).opacity);
    const ring = parse(cs.outlineColor);
    const [hi, lo] = [lum(over({ ...ring, a: ring.a * alpha }, surface)), lum(surface)].sort((a, b) => b - a);
    return (hi + 0.05) / (lo + 0.05);
  });
}

/** axe-core's serious and critical findings on the page as it stands. */
async function seriousAxe(page: Page) {
  const { violations } = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"])
    .analyze();
  return violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
}

test.describe("theme contrast", () => {
  // The three theme settings, with the OS preference emulated both ways for
  // System. The explicit choices run against the *opposite* OS preference, so
  // a token block leaking through the wrong guard would show up here.
  const MODES = [
    { name: "Light", value: "light", os: "dark", dark: false },
    { name: "Dark", value: "dark", os: "light", dark: true },
    { name: "System, OS light", value: "system", os: "light", dark: false },
    { name: "System, OS dark", value: "system", os: "dark", dark: true },
  ] as const;

  // No allowance in either palette: 1.6.1 tolerated the light OK/Empty/
  // Unknown badges and placeholder down to 4.0:1; their tokens now clear 4.5.
  const failures = (low: { detail: string }[]) => low.map((l) => l.detail);

  const PAGE_BG = { dark: "rgb(13, 17, 23)", light: "rgb(244, 246, 248)" };

  // Firefox's UA sheet dims ::placeholder to 54%; Chromium's leaves it at 1, so
  // on Chromium alone the `opacity: 1` in styles.css guards nothing anyone can
  // see, and deleting it would pass. Firefox's default is re-created here in a
  // cascade layer: layered author rules lose to unlayered ones, so the app's
  // own rule still wins - for exactly as long as it exists.
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      document.addEventListener("DOMContentLoaded", () => {
        const style = document.createElement("style");
        style.textContent = "@layer firefox-ua { ::placeholder { opacity: 0.54; } }";
        document.head.append(style);
      });
    });
  });

  /** Sign in under `mode` and confirm which palette won before measuring. */
  async function signInAs(page: Page, mode: (typeof MODES)[number]) {
    await page.emulateMedia({ colorScheme: mode.os });
    await bootstrapAndLogin(page);
    await page.getByLabel("Theme").selectOption(mode.value);
    await expect(page.locator("body")).toHaveCSS(
      "background-color",
      mode.dark ? PAGE_BG.dark : PAGE_BG.light,
    );
  }

  for (const mode of MODES) {
    test(`${mode.name}: bay numbers and every tab's text stay readable`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: mode.os });
      await bootstrapAndLogin(page);
      await page.getByLabel("Theme").selectOption(mode.value);

      // Every route to a palette must land on the same tokens; the page
      // background is the cheapest witness of which block won.
      await expect(page.locator("body")).toHaveCSS(
        "background-color",
        mode.dark ? "rgb(13, 17, 23)" : "rgb(244, 246, 248)",
      );
      // ...and the UA's own parts (form controls, scrollbars, the search
      // field's clear button) must follow the same choice, not just the tokens.
      await expect(page.locator("html")).toHaveCSS("color-scheme", mode.dark ? "dark" : "normal");

      // The 1.6.0 regression: a tile is a <button>, buttons do not inherit
      // colour, and .bay-no had none of its own, so the bay numbers rendered
      // the UA's black on the dark panel. Plain, selected and hovered tiles.
      await expect(page.locator(".bay-no")).toHaveCount(16);
      await page.getByRole("button", { name: /^Bay 1\b/ }).click();
      await page.getByRole("button", { name: /^Bay 2\b/ }).hover();
      const numbers = await lowContrast(page, ".bay-no");
      expect(numbers.checked).toBe(16);
      expect(numbers.low.map((l) => l.detail), "bay numbers").toEqual([]);

      const map = await lowContrast(page, ".topbar *, main *");
      expect(map.checked).toBeGreaterThan(100);
      // The Theme select in the top bar and Bay 1's "Identify for" select.
      expect(map.controls).toBeGreaterThanOrEqual(2);
      expect(failures(map.low), "drive map tab").toEqual([]);
      // The search field's placeholder, at full strength despite the emulated
      // Firefox default (see the beforeEach).
      expect(map.placeholderOpacity, "::placeholder opacity").toEqual(["1"]);

      // Typed text in the search field, not just its placeholder. A query that
      // matches nothing dims every tile, so it is cleared again straight after.
      const search = page.getByLabel("Search bays");
      await search.fill("no-such-drive");
      const typed = await lowContrast(page, 'input[type="search"]');
      expect(typed.controls).toBe(1);
      expect(failures(typed.low), "typed search text").toEqual([]);
      await search.fill("");

      await page.getByRole("tab", { name: "Chassis" }).click();
      await expect(page.getByText("Chassis health")).toBeVisible({ timeout: 15_000 });
      const chassis = await lowContrast(page, "main *");
      expect(chassis.checked).toBeGreaterThan(50);
      expect(failures(chassis.low), "chassis tab").toEqual([]);

      await page.getByRole("tab", { name: "Diagnostics" }).click();
      await expect(page.locator("pre.raw").first()).toContainText("app_version");
      const diag = await lowContrast(page, "main *");
      expect(diag.checked).toBeGreaterThan(5);
      expect(diag.controls).toBeGreaterThanOrEqual(1); // the raw-page select
      expect(failures(diag.low), "diagnostics tab").toEqual([]);

      // Typed (masked) password text. Filled but never submitted, so the
      // credential the later tests depend on is untouched.
      await page.getByRole("button", { name: "Change password" }).click();
      const dialog = page.getByRole("dialog");
      await dialog.getByLabel("Current password").fill("typed-text-probe");
      const modal = await lowContrast(page, ".modal *");
      expect(modal.checked).toBeGreaterThan(5);
      expect(modal.controls).toBeGreaterThanOrEqual(1);
      expect(failures(modal.low), "change-password dialog").toEqual([]);
    });

    // Review 10-29 L3: the states a healthy test backend never shows. Each is
    // forced in the browser - the backend's answers are rewritten, never its
    // state - and each is first proved present, then measured on its own, so
    // a notice that failed to render cannot pass as "nothing below 4.5:1".
    test(`${mode.name}: warning, error and info notices and the state badges stay readable`, async ({
      page,
    }) => {
      let failBays = false;
      await page.route("**/api/enclosures/*/bays", async (route) => {
        if (failBays) {
          await route.fulfill({ status: 503, json: { detail: "e2e: bay poll refused" } });
          return;
        }
        const body = await (await route.fetch()).json();
        // Both source errors: two .notice.warn banners and the header's
        // "degraded" badge.
        body.sources.slots_error = "e2e: enclosure not readable";
        body.sources.truenas_error = "e2e: connection refused";
        // TrueNAS's over-temperature alert on Bay 1: the "too hot" badge.
        body.bays.find((b: { ses_slot: number }) => b.ses_slot === 0).smart.over_temperature = true;
        await route.fulfill({ json: body });
      });
      await page.route("**/api/enclosures/*/chassis", async (route) => {
        const body = await (await route.fetch()).json();
        // Banks at different steps: the one state that renders .notice.info.
        const fans = (body.elements ?? []).filter(
          (e: { element_type: string; is_overall: boolean }) =>
            e.element_type === "Cooling" && !e.is_overall,
        );
        if (fans.length > 0) fans[0].speed_code = 4;
        await route.fulfill({ json: body });
      });
      await signInAs(page, mode);

      const warn = page.locator("main > .notice.warn");
      await expect(warn).toHaveCount(2, { timeout: 10_000 });
      const degraded = page.getByTestId("freshness").locator("summary .badge.warning");
      await expect(degraded).toHaveText("▲ degraded");
      await page.getByRole("button", { name: /^Bay 1\b/ }).click();
      const tooHot = page.getByTestId("bay-detail").locator(".badge.warning");
      await expect(tooHot).toHaveText("▲ too hot");

      const notices = await lowContrast(page, "main > .notice.warn");
      expect(notices.checked).toBe(2);
      expect(failures(notices.low), ".notice.warn").toEqual([]);
      const badges = await lowContrast(
        page,
        '[data-testid="freshness"] .badge.warning, [data-testid="bay-detail"] .badge.warning',
      );
      expect(badges.checked).toBe(2);
      expect(failures(badges.low), "degraded / too hot badges").toEqual([]);
      // And everything around them, as the first test measures the healthy page.
      const map = await lowContrast(page, ".topbar *, main *");
      expect(failures(map.low), "drive map with every warning up").toEqual([]);

      // A poll that fails outright raises the page's error banner. The rows
      // and the warnings above stay on the last good answer.
      failBays = true;
      const error = page.locator("main > .notice.error");
      await expect(error).toContainText("e2e: bay poll refused", { timeout: 10_000 });
      const errors = await lowContrast(page, "main > .notice.error");
      expect(errors.checked).toBe(1);
      expect(failures(errors.low), ".notice.error").toEqual([]);

      await page.getByRole("tab", { name: "Chassis" }).click();
      const info = page.locator("main .notice.info");
      await expect(info).toContainText("different speed steps", { timeout: 15_000 });
      const infos = await lowContrast(page, "main .notice.info");
      expect(infos.checked).toBe(1);
      expect(failures(infos.low), ".notice.info").toEqual([]);
    });

    test(`${mode.name}: axe finds nothing serious or critical on any screen`, async ({ page }) => {
      await signInAs(page, mode);
      await page.getByRole("button", { name: /^Bay 1\b/ }).click();
      await expect(page.getByTestId("bay-detail")).toBeVisible();
      expect(await seriousAxe(page), "drive map with a bay selected").toEqual([]);

      // Tiles dimmed by a search that does not match them, one of them keyboard-
      // focused. Known and pre-existing: the dimmed text itself is below 4.5:1
      // (axe color-contrast; 1.5-2.9:1 measured, the same before and after the
      // tile dimmed its children instead of itself), so that one rule is set
      // aside here - but only for text inside a dimmed tile. Anything else
      // serious, or contrast anywhere else, still fails.
      await page.getByLabel("Search bays").fill("K1A00001");
      await expect(page.locator(".bay.dim")).toHaveCount(15);
      const dimmedTile = page.getByRole("button", { name: /^Bay 2\b/ });
      await dimmedTile.focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      await expect(dimmedTile).toBeFocused();
      const outsideDim = (await seriousAxe(page)).flatMap((v) => {
        if (!v.startsWith("color-contrast ")) return [v];
        const nodes = v.slice(v.indexOf(": ") + 2).split(", ");
        return nodes.filter((n) => !/\.dim\.bay > /.test(n)).map((n) => `color-contrast: ${n}`);
      });
      expect(outsideDim, "drive map with a search dimming tiles").toEqual([]);
      await page.getByLabel("Search bays").fill("");

      // Every scrollable wrapper is a tab stop, so each is a named region
      // rather than an anonymous group (review of #61, F3).
      const unnamedRegions = () =>
        page.locator(".table-scroll, pre.raw").evaluateAll((els) =>
          els
            .filter((el) => el.getAttribute("role") !== "region" || !el.getAttribute("aria-label")?.trim())
            .map((el) => el.outerHTML.slice(0, 60)),
        );

      await page.getByRole("tab", { name: "Chassis" }).click();
      await expect(page.getByText("Chassis health")).toBeVisible({ timeout: 15_000 });
      expect(await seriousAxe(page), "chassis tab").toEqual([]);
      await expect(page.getByRole("region", { name: "Cooling table" })).toBeVisible();
      expect(await page.locator(".table-scroll").count()).toBeGreaterThanOrEqual(4);
      expect(await unnamedRegions(), "chassis scroll regions without a name").toEqual([]);

      await page.getByRole("tab", { name: "Diagnostics" }).click();
      await expect(page.locator("pre.raw").first()).toContainText("app_version");
      await page.getByRole("button", { name: "Run" }).click();
      await expect(page.locator("pre.raw")).toHaveCount(2);
      expect(await seriousAxe(page), "diagnostics tab").toEqual([]);
      await expect(page.getByRole("region", { name: "Diagnostics output" })).toContainText("app_version");
      await expect(page.getByRole("region", { name: "Raw page output" })).toBeVisible();
      await expect(page.getByRole("region", { name: "Audit log table" })).toBeVisible();
      expect(await unnamedRegions(), "diagnostics scroll regions without a name").toEqual([]);

      await page.getByRole("button", { name: "Change password" }).click();
      await expect(page.getByRole("dialog")).toBeVisible();
      expect(await seriousAxe(page), "change-password dialog").toEqual([]);
      await page.keyboard.press("Escape");

      await page.getByRole("button", { name: /Sign out/ }).click();
      await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
      expect(await seriousAxe(page), "login screen").toEqual([]);
    });

    test(`${mode.name}: every keyboard focus ring reaches 3:1 against its surface`, async ({
      page,
    }) => {
      await signInAs(page, mode);
      const rings: Record<string, number> = {};
      const ring = async (name: string, target: Locator) => {
        rings[name] = Number((await focusRing(page, target)).toFixed(2));
      };
      // Drive map: tab, search, theme select, freshness summary, and a bay
      // tile plain, selected and dimmed by a search that does not match it.
      await ring("tab", page.getByRole("tab", { name: "Drive map" }));
      await ring("search", page.getByLabel("Search bays"));
      await ring("theme select", page.getByLabel("Theme"));
      await ring("freshness summary", page.getByTestId("freshness").locator("summary"));
      await ring("bay tile", page.getByRole("button", { name: /^Bay 2\b/ }));
      await page.getByRole("button", { name: /^Bay 1\b/ }).click();
      await ring("selected bay tile", page.getByRole("button", { name: /^Bay 1\b/ }));
      await ring("identify select", page.getByLabel("Identify for"));
      await ring("identify button", page.getByRole("button", { name: "Identify", exact: true }));
      await page.getByLabel("Search bays").fill("K1A00001");
      await ring("dimmed bay tile", page.getByRole("button", { name: /^Bay 2\b/ }));
      // ...and while it holds focus it must still look like a non-match: its
      // content stays dimmed even though its ring is at full strength.
      // Effective opacity of the bay number, through every ancestor.
      const strength = (name: RegExp) =>
        page.getByRole("button", { name }).locator(".bay-no").evaluate((el) => {
          let a = 1;
          for (let n: Element | null = el; n; n = n.parentElement) a *= Number(getComputedStyle(n).opacity);
          return a;
        });
      expect(await strength(/^Bay 2\b/), "focused dimmed tile's content").toBeLessThan(0.5);
      expect(await strength(/^Bay 1\b/), "control: the matching tile").toBe(1);
      await page.getByLabel("Search bays").fill("");

      await page.getByRole("tab", { name: "Chassis" }).click();
      await expect(page.getByText("Chassis health")).toBeVisible({ timeout: 15_000 });
      await ring("chassis table", page.locator(".table-scroll").first());

      await page.getByRole("tab", { name: "Diagnostics" }).click();
      await expect(page.locator("pre.raw").first()).toContainText("app_version");
      await ring("diagnostics output", page.locator("pre.raw").first());
      await ring("raw-page select", page.getByLabel("Page", { exact: true }));

      await page.getByRole("button", { name: "Change password" }).click();
      const dialog = page.getByRole("dialog");
      await ring("dialog field", dialog.getByLabel("Current password"));
      await ring("dialog primary button", dialog.getByRole("button", { name: "Change password" }));
      await ring("dialog secondary button", dialog.getByRole("button", { name: "Cancel" }));

      const low = Object.entries(rings).filter(([, r]) => r < 3);
      expect(Object.keys(rings)).toHaveLength(15);
      expect(low, "focus rings below 3:1 (0 = no ring drawn)").toEqual([]);
    });

    test(`${mode.name}: the login screen, its notices and the unauthenticated badge stay readable`, async ({
      page,
    }) => {
      // The theme is chosen while signed in; it is kept in localStorage, so the
      // login screen and the reload below are drawn in it too.
      await signInAs(page, mode);

      // A sign-out whose POST never arrives lands on the login screen with the
      // unmodified .notice explaining that the session may still be live.
      await page.route("**/api/auth/logout", (route) => route.abort());
      await page.getByRole("button", { name: /Sign out/ }).click();
      await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
      await expect(page.locator(".login > .notice:not(.error)")).toContainText(
        "Sign out did not complete",
      );
      await expect(page.locator("body")).toHaveCSS(
        "background-color",
        mode.dark ? PAGE_BG.dark : PAGE_BG.light,
      );

      // A refused sign-in for .notice.error, answered in the browser: a real
      // wrong password would count against the server's login rate limit,
      // which every later test's sign-in shares.
      await page.route("**/api/auth/login", (route) =>
        route.fulfill({ status: 401, json: { detail: "invalid username or password" } }),
      );
      await page.getByLabel("Username").fill(USER);
      await page.getByLabel("Password", { exact: true }).fill("typed-text-probe");
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(page.locator(".login > .notice.error")).toContainText(
        "invalid username or password",
      );

      const notices = await lowContrast(page, ".login > .notice");
      expect(notices.checked).toBe(2);
      expect(failures(notices.low), "login notices").toEqual([]);
      const login = await lowContrast(page, ".login *");
      expect(login.checked).toBeGreaterThan(6);
      expect(login.controls).toBe(2); // typed username and (masked) password
      expect(failures(login.low), "login screen").toEqual([]);

      // KTN_AUTH_REQUIRED=false shows "unauthenticated" where the account
      // buttons would be. Only the status answer is rewritten.
      await page.route("**/api/auth/status", (route) =>
        route.fulfill({
          json: {
            auth_required: false,
            anonymous_ident_allowed: false,
            needs_bootstrap: false,
            user: null,
          },
        }),
      );
      await page.reload();
      const badge = page.locator(".topbar .badge.warning");
      await expect(badge).toHaveText("unauthenticated");
      await expect(page.locator("body")).toHaveCSS(
        "background-color",
        mode.dark ? PAGE_BG.dark : PAGE_BG.light,
      );
      const badges = await lowContrast(page, ".topbar .badge.warning");
      expect(badges.checked).toBe(1);
      expect(failures(badges.low), "unauthenticated badge").toEqual([]);
      const top = await lowContrast(page, ".topbar *");
      expect(failures(top.low), "top bar without a session").toEqual([]);
    });
  }

  test("the contrast check sees a text-fill override and a dimmed placeholder", async ({
    page,
  }, testInfo) => {
    // A check of the checker (review 10-29 L1/L2). Both overrides leave `color`
    // exactly as shipped, which is the measurement the check used to rely on.
    test.skip(testInfo.project.name !== "desktop", "one project is enough for a self-test");
    await signInAs(page, MODES[0]);
    const search = page.getByLabel("Search bays");
    const field = 'input[type="search"]';

    const clean = await lowContrast(page, field);
    expect(failures(clean.low), "control: the placeholder as shipped").toEqual([]);
    expect(clean.placeholderOpacity).toEqual(["1"]);

    // L2: the placeholder's own opacity, which dims its text but not the field.
    const dim = await page.addStyleTag({ content: `${field}::placeholder { opacity: 0.4; }` });
    const dimmed = await lowContrast(page, field);
    expect(dimmed.placeholderOpacity).toEqual(["0.4"]);
    expect(dimmed.low.map((l) => l.label)).toEqual(["::placeholder"]);
    await dim.evaluate((el) => (el as Element).remove());

    // L1: glyphs painted in the field's own background colour.
    await search.fill("typed");
    expect(failures((await lowContrast(page, field)).low), "control: typed text").toEqual([]);
    await page.addStyleTag({ content: `${field} { -webkit-text-fill-color: #f8fafc; }` });
    const filled = await lowContrast(page, field);
    expect(filled.low.map((l) => l.label)).toEqual(["input[type=search] value (text-fill)"]);
    await search.fill("");
  });
});

test.describe("keyboard", () => {
  test("the password dialog keeps focus, closes on Escape and hands focus back", async ({
    page,
  }) => {
    await bootstrapAndLogin(page);
    const opener = page.getByRole("button", { name: "Change password" });
    await opener.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByLabel("Current password")).toBeFocused();
    // The page behind is inert: nothing in it can take focus or be clicked.
    await expect(page.locator("header.topbar")).toHaveAttribute("inert", "");
    await expect(page.locator("main")).toHaveAttribute("inert", "");
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press("Tab");
      const inDialog = await page.evaluate(
        () => !document.activeElement || document.activeElement === document.body
          || !!document.activeElement.closest('[role="dialog"]'),
      );
      expect(inDialog, `Tab ${i + 1} left the dialog`).toBe(true);
    }
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(opener).toBeFocused();
    await expect(page.locator("main")).not.toHaveAttribute("inert");
  });

  test("a bay tile is selected from the keyboard and announces it", async ({ page }) => {
    await bootstrapAndLogin(page);
    const tile = page.getByRole("button", { name: /^Bay 3\b/ });
    await tile.focus();
    await page.keyboard.press("Enter");
    await expect(tile).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("bay-detail")).toContainText("Bay 3");
    await page.getByRole("button", { name: /^Bay 4\b/ }).focus();
    await page.keyboard.press("Space");
    await expect(tile).toHaveAttribute("aria-pressed", "false");
  });

  test("Escape still closes the password dialog after a request fails", async ({ page }) => {
    // Review of #61, F1: Enter on the submit button disables it, Chromium drops
    // focus to <body>, and a listener on the dialog never hears Escape again.
    // The request is held in the browser and refused there, so the suite's
    // credential is never touched.
    await bootstrapAndLogin(page);
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/auth/password", async (route) => {
      await held;
      await route.fulfill({ status: 400, json: { detail: "e2e: current password is wrong" } });
    });
    const opener = page.getByRole("button", { name: "Change password" });
    await opener.click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Current password").fill("e2e-not-the-password");
    await dialog.getByLabel("New password", { exact: true }).fill("e2e-another-password");
    await dialog.getByLabel("Confirm new password").fill("e2e-another-password");
    await dialog.getByRole("button", { name: "Change password" }).focus();
    await page.keyboard.press("Enter");
    await expect(dialog.getByRole("button", { name: "Changing…" })).toBeVisible();

    // In flight: Escape is ignored, so a change cannot be abandoned half-sent.
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(1);

    release();
    await expect(dialog.getByRole("alert")).toContainText("e2e: current password is wrong");
    // Focus is back inside the dialog, wherever the disabled button left it.
    await expect
      .poll(() => page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]')))
      .toBe(true);

    // And Escape works even from <body>, the state the review measured.
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(opener).toBeFocused();
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
