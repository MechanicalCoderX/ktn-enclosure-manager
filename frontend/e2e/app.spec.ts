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
        page.getByRole("listitem", { name: new RegExp(`^Bay ${BAY},.*identify active`, "i") }),
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

/**
 * WCAG 2.x contrast of every visible text element under `selector`, measured
 * against what is actually painted behind it: the ancestor chain's backgrounds
 * are composited top-down (alpha and group opacity included), so a transparent
 * element is judged against the first painted surface beneath it rather than
 * against nothing. Disabled controls and search-dimmed tiles are skipped (WCAG
 * exempts inactive components; dimming is the deliberate "not a match" cue).
 * Returns how many elements were measured and those below 4.5:1 (3:1 large).
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
    const judge = (el: Element, label: string, color: string) => {
      const cs = getComputedStyle(el);
      const text = paint(el, parse(color));
      const bg = paint(el, null);
      const [hi, lo] = [lum(text), lum(bg)].sort((a, b) => b - a);
      const ratio = (hi + 0.05) / (lo + 0.05);
      const size = parseFloat(cs.fontSize);
      const large = size >= 24 || (size >= 18.66 && Number(cs.fontWeight) >= 700);
      checked++;
      if (ratio < (large ? 3 : 4.5))
        low.push({
          label,
          ratio,
          detail: `${label}: ${hex(text)} on ${hex(bg)} = ${ratio.toFixed(2)}:1 at ${size}px`,
        });
    };
    for (const el of document.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0 || el.closest(":disabled, .dim, option")) continue;
      if (el instanceof HTMLInputElement) {
        if (el.placeholder && !el.value)
          judge(el, "::placeholder", getComputedStyle(el, "::placeholder").color);
        continue;
      }
      if ([...el.childNodes].some((n) => n.nodeType === 3 && n.textContent?.trim()))
        judge(el, `${el.className || el.tagName.toLowerCase()} "${el.textContent?.trim().slice(0, 24)}"`,
              getComputedStyle(el).color);
    }
    return { checked, low };
  }, selector);
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

  // Shortfalls that predate the dark-theme fix and belong to the light
  // palette, which that fix deliberately left untouched: the 10px
  // OK/Empty/Unknown badges (4.43 and 4.07:1) and Chromium's own placeholder
  // grey (4.40:1). Tolerated only in light and only down to 4.0, so a further
  // drop still fails; dark has no such allowance.
  const KNOWN_LIGHT = /^badge (ok|empty|unknown) "|^span "[✓○?]"$|^::placeholder$/;
  const failures = (dark: boolean, low: { label: string; ratio: number; detail: string }[]) =>
    low
      .filter((l) => dark || !(KNOWN_LIGHT.test(l.label) && l.ratio >= 4.0))
      .map((l) => l.detail);

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

      // The 1.6.0 regression: a tile is a <button>, buttons do not inherit
      // colour, and .bay-no had none of its own, so the bay numbers rendered
      // the UA's black on the dark panel. Plain, selected and hovered tiles.
      await expect(page.locator(".bay-no")).toHaveCount(16);
      await page.getByRole("listitem", { name: /^Bay 1, / }).click();
      await page.getByRole("listitem", { name: /^Bay 2, / }).hover();
      const numbers = await lowContrast(page, ".bay-no");
      expect(numbers.checked).toBe(16);
      expect(numbers.low.map((l) => l.detail), "bay numbers").toEqual([]);

      const map = await lowContrast(page, ".topbar *, main *");
      expect(map.checked).toBeGreaterThan(100);
      expect(failures(mode.dark, map.low), "drive map tab").toEqual([]);

      await page.getByRole("tab", { name: "Chassis" }).click();
      await expect(page.getByText("Chassis health")).toBeVisible({ timeout: 15_000 });
      const chassis = await lowContrast(page, "main *");
      expect(chassis.checked).toBeGreaterThan(50);
      expect(failures(mode.dark, chassis.low), "chassis tab").toEqual([]);

      await page.getByRole("tab", { name: "Diagnostics" }).click();
      await expect(page.locator("pre.raw").first()).toContainText("app_version");
      const diag = await lowContrast(page, "main *");
      expect(diag.checked).toBeGreaterThan(5);
      expect(failures(mode.dark, diag.low), "diagnostics tab").toEqual([]);

      await page.getByRole("button", { name: "Change password" }).click();
      const modal = await lowContrast(page, ".modal *");
      expect(modal.checked).toBeGreaterThan(5);
      expect(failures(mode.dark, modal.low), "change-password dialog").toEqual([]);
    });
  }
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
