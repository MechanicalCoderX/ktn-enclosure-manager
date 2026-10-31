import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { Bay, BaySources, Chassis, Enclosure, IdentDuration } from "./types";
import {
  BayDetail,
  ChassisView,
  DiagnosticsView,
  EnclosureMap,
  FreshnessSummary,
} from "./views";

type Tab = "map" | "chassis" | "diagnostics";
type Theme = "system" | "light" | "dark";

const POLL_BAYS_MS = 5000;
const POLL_CHASSIS_MS = 30000;

/**
 * Poll on an interval, but only while the tab is actually being looked at,
 * and refresh immediately when it becomes visible again.
 *
 * Without the visibility check a tab left open overnight kept asking every
 * five seconds - about 17,000 requests a day, each one composing all fifteen
 * bays server-side - to redraw a page nobody was watching. Resuming with an
 * immediate call means the first thing you see on returning is current, not
 * up to one interval stale.
 */
function usePolling(callback: () => void, intervalMs: number, active: boolean): void {
  useEffect(() => {
    if (!active) return;

    let timer: number | undefined;

    const stop = () => {
      if (timer !== undefined) {
        clearInterval(timer);
        timer = undefined;
      }
    };

    const start = () => {
      if (timer !== undefined) return;
      timer = window.setInterval(callback, intervalMs);
    };

    const onVisibilityChange = () => {
      if (document.hidden) {
        stop();
      } else {
        callback();
        start();
      }
    };

    if (!document.hidden) {
      callback();
      start();
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [callback, intervalMs, active]);
}

/**
 * When a bay's timed IDENT ends, on the browser's clock - or null when it has
 * no timer ("until cleared", an external LED, or no IDENT at all).
 *
 * `ident_expires_at` is on the server's clock. `clockOffsetMs` (browser minus
 * server, measured from the response's own `generated_at`) moves it onto this
 * one, so a browser clock that runs fast cannot darken the marker early and one
 * that runs slow cannot leave it lit late. An unparseable stamp yields NaN,
 * which every comparison below treats as "no timer": the server's word stands.
 */
function identDeadline(bay: Bay, clockOffsetMs: number): number | null {
  if (!bay.locate || !bay.ident_expires_at) return null;
  return Date.parse(bay.ident_expires_at) + clockOffsetMs;
}

/**
 * Stop a timed IDENT marker at its expiry rather than on the next poll.
 *
 * The server clears the LED on its own timer, but the map only learned of it on
 * the first bay poll afterwards - up to POLL_BAYS_MS late, so the marker kept
 * blinking after the shelf had gone dark. Instead this schedules one timeout
 * for the earliest pending expiry; when it fires the caller hides every lapsed
 * marker and re-reads /bays once, so the server is still the authority on what
 * happens next. A new payload that moves an expiry later (Identify re-triggered
 * or extended) recomputes the deadline and cancels the pending timeout, so a
 * marker is never put out early.
 *
 * Returns the instant lapsed markers are judged against. `expiredThrough`
 * covers a timeout that fires a millisecond before Date.now() reaches its
 * deadline, which would otherwise leave the marker lit until the next poll.
 */
function useIdentExpiry(bays: Bay[], clockOffsetMs: number, onExpire: () => void): number {
  const [expiredThrough, setExpiredThrough] = useState(0);
  const now = Math.max(Date.now(), expiredThrough);
  const next = bays.reduce<number | null>((soonest, bay) => {
    const at = identDeadline(bay, clockOffsetMs);
    // Only future deadlines: a payload that still reports an already-lapsed
    // timer (the server clears on a ~1 s tick) must not re-arm a refetch loop.
    if (at === null || !(at > now)) return soonest;
    return soonest === null || at < soonest ? at : soonest;
  }, null);

  useEffect(() => {
    if (next === null) return;
    const timer = window.setTimeout(() => {
      setExpiredThrough(next);
      onExpire();
    }, next - Date.now());
    return () => clearTimeout(timer);
  }, [next, onExpire]);

  return now;
}

/**
 * Change the signed-in account's password.
 *
 * The endpoint and the API client for this existed from the start but nothing
 * ever rendered them, so the only way to change a password was to call the API
 * by hand. Anyone who suspected their credentials were exposed could not act
 * on it from the application - which is the one moment the feature exists for.
 */
function ChangePasswordDialog({
  onClose,
  onChanged,
  onRevoked,
}: {
  onClose: () => void;
  onChanged: () => void;
  onRevoked: () => void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const firstField = useRef<HTMLInputElement>(null);

  // Escape is heard on the document, not on the dialog: pressing Enter on a
  // button that then disables itself drops Chromium's focus to <body>, outside
  // the dialog, where a listener on the backdrop never sees the key. Ignored
  // while a request is in flight, so a change cannot be abandoned half-sent.
  useEffect(() => {
    if (busy) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  // When a request ends with the dialog still open (it failed), put focus back
  // inside it if the disabled button let it fall out, so Tab starts from the
  // top of the form rather than wherever the browser parked it.
  const wasBusy = useRef(false);
  useEffect(() => {
    if (wasBusy.current && !busy && !dialogRef.current?.contains(document.activeElement)) {
      firstField.current?.focus();
    }
    wasBusy.current = busy;
  }, [busy]);

  const revokeAll = async () => {
    setError(null);
    setBusy(true);
    try {
      await api.revokeSessions();
      onRevoked();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (next !== confirm) {
      setError("New passwords do not match");
      return;
    }
    if (next.length < 12) {
      setError("New password must be at least 12 characters");
      return;
    }
    if (next === current) {
      setError("New password must differ from the current one");
      return;
    }
    setBusy(true);
    try {
      await api.changePassword(current, next);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true"
         aria-labelledby="cp-title" ref={dialogRef}>
      <div className="modal">
        <h2 id="cp-title">Change password</h2>
        <p className="sub">
          Every other session for this account is signed out, so you will be
          asked to sign in again.
        </p>
        <form onSubmit={submit}>
          <label htmlFor="cp-current">Current password</label>
          <input id="cp-current" type="password" autoComplete="current-password"
                 ref={firstField} value={current} onChange={(e) => setCurrent(e.target.value)}
                 required autoFocus />

          <label htmlFor="cp-new">New password</label>
          <input id="cp-new" type="password" autoComplete="new-password"
                 minLength={12} value={next}
                 onChange={(e) => setNext(e.target.value)} required />

          <label htmlFor="cp-confirm">Confirm new password</label>
          <input id="cp-confirm" type="password" autoComplete="new-password"
                 minLength={12} value={confirm}
                 onChange={(e) => setConfirm(e.target.value)} required />

          {error && <div className="notice error" role="alert">{error}</div>}

          <div className="modal-actions">
            <button type="button" className="btn secondary" onClick={onClose}
                    disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="btn" disabled={busy}>
              {busy ? "Changing…" : "Change password"}
            </button>
          </div>
        </form>

        <hr />
        <p className="sub">
          Think a session was stolen but the password itself is fine? Sign this
          account out everywhere — every session ends, including this one, and
          you sign back in.
        </p>
        <button type="button" className="btn secondary" onClick={revokeAll}
                disabled={busy}>
          Sign out everywhere
        </button>
      </div>
    </div>
  );
}

function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem("ktn-theme") as Theme) ?? "system",
  );
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    localStorage.setItem("ktn-theme", theme);
  }, [theme]);
  return [theme, setTheme];
}

// ------------------------------------------------------------------- login

function LoginScreen({
  needsBootstrap,
  onAuthenticated,
  notice,
}: {
  needsBootstrap: boolean;
  onAuthenticated: (user: string) => void;
  notice?: string | null;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (needsBootstrap) {
        if (password !== confirm) throw new Error("passwords do not match");
        await api.bootstrap(username, password);
      }
      const result = await api.login(username, password);
      onAuthenticated(result.user);
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="panel login" onSubmit={submit}>
        <h2>KTN Enclosure Manager</h2>
        <div className="sub">
          {needsBootstrap
            ? "First run — create the administrator account."
            : "Sign in to continue."}
        </div>
        {notice && <div className="notice">{notice}</div>}
        {error && <div className="notice error" role="alert">{error}</div>}
        <label htmlFor="u">Username</label>
        <input id="u" type="text" autoComplete="username" value={username}
               onChange={(e) => setUsername(e.target.value)} required />
        <label htmlFor="p">Password</label>
        <input id="p" type="password" value={password}
               autoComplete={needsBootstrap ? "new-password" : "current-password"}
               onChange={(e) => setPassword(e.target.value)} required />
        {needsBootstrap && (
          <>
            <label htmlFor="c">Confirm password</label>
            <input id="c" type="password" autoComplete="new-password" value={confirm}
                   onChange={(e) => setConfirm(e.target.value)} required />
            <div className="sub" style={{ marginTop: 8 }}>Minimum 12 characters.</div>
          </>
        )}
        <button className="btn" type="submit" disabled={busy}>
          {busy ? "Working…" : needsBootstrap ? "Create account" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

// --------------------------------------------------------------------- app

export function App() {
  const [user, setUser] = useState<string | null>(null);
  const [needsBootstrap, setNeedsBootstrap] = useState(false);
  const [ready, setReady] = useState(false);

  const [enclosures, setEnclosures] = useState<Enclosure[]>([]);
  const [selectedEnclosure, setSelectedEnclosure] = useState<string | null>(null);
  const [bays, setBays] = useState<Bay[]>([]);
  const [chassis, setChassis] = useState<Chassis | null>(null);
  const [selectedSlot, setSelectedSlot] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("map");
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  // The whole freshness block, kept together rather than split into a stamp and
  // an error string. They describe four separate clocks (§29) and every consumer
  // - the header summary, each detail section, both degradation banners - needs
  // to pick a different one out of it; the previous shape kept only the fastest
  // source's timestamp and discarded the rest on arrival.
  const [sources, setSources] = useState<BaySources | null>(null);
  // Browser clock minus server clock, from the last /bays response. Zero when
  // the server did not say (the browser's clock is then the only one there is).
  const [clockOffsetMs, setClockOffsetMs] = useState(0);
  const [busy, setBusy] = useState(false);
  const [authRequired, setAuthRequired] = useState(true);
  const [anonIdentAllowed, setAnonIdentAllowed] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);
  // Closing the dialog hands focus back to the button that opened it. It has
  // to wait for the commit: the button is inert while the dialog is up.
  const passwordButton = useRef<HTMLButtonElement>(null);
  const wasChangingPassword = useRef(false);
  useEffect(() => {
    if (wasChangingPassword.current && !changingPassword) passwordButton.current?.focus();
    wasChangingPassword.current = changingPassword;
  }, [changingPassword]);
  const [signOutNotice, setSignOutNotice] = useState<string | null>(null);
  const [theme, setTheme] = useTheme();

  useEffect(() => {
    api
      .authStatus()
      .then((s) => {
        setNeedsBootstrap(s.needs_bootstrap);
        setAuthRequired(s.auth_required);
        setAnonIdentAllowed(s.anonymous_ident_allowed);
        setUser(s.user);
      })
      .catch(() => undefined)
      .finally(() => setReady(true));
  }, []);

  useEffect(() => {
    if (!user) return;
    api
      .enclosures()
      .then((list) => {
        setEnclosures(list);
        if (list.length > 0) setSelectedEnclosure((prev) => prev ?? list[0].logical_id);
      })
      .catch((e: ApiError) => setError(e.message));
  }, [user]);

  const refreshBays = useCallback(async () => {
    if (!selectedEnclosure) return;
    try {
      const response = await api.bays(selectedEnclosure);
      const receivedAt = Date.now();
      const serverNow = Date.parse(response.generated_at ?? "");
      setBays(response.bays);
      setSources(response.sources);
      // Includes the response's transit time, so on a LAN the local deadline
      // lands a few milliseconds after the server's - still ahead of the
      // server's own clear, which runs on a one-second tick plus the write.
      setClockOffsetMs(Number.isFinite(serverNow) ? receivedAt - serverNow : 0);
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setUser(null);
      else setError(e instanceof Error ? e.message : String(e));
    }
  }, [selectedEnclosure]);

  usePolling(
    refreshBays,
    POLL_BAYS_MS,
    Boolean(user && selectedEnclosure),
  );

  const identNow = useIdentExpiry(bays, clockOffsetMs, refreshBays);

  const loadChassis = useCallback(() => {
    if (!selectedEnclosure) return;
    void api.chassis(selectedEnclosure).then(setChassis).catch(() => undefined);
  }, [selectedEnclosure]);

  usePolling(
    loadChassis,
    POLL_CHASSIS_MS,
    Boolean(user && selectedEnclosure && tab === "chassis"),
  );

  const signOut = () =>
    api
      .logout()
      .catch((e) => {
        // Server-side revocation is the real sign-out; without this .catch a
        // failed POST (backend restarting, network blip) was an unhandled
        // rejection — the button silently did nothing while the user walked
        // away believing the session was dead. Say so, on the screen the user
        // actually lands on: with auth required that is the login screen,
        // which renders signOutNotice, not the dashboard's error banner.
        const detail = e instanceof Error ? e.message : String(e);
        const message =
          `Sign out did not complete (${detail}). The server may still honour ` +
          "this session — sign in and sign out again, or use " +
          "Change password → Sign out everywhere.";
        if (authRequired) setSignOutNotice(message);
        else setError(message);
      })
      // Local state is cleared regardless: the UI must reflect what the user
      // asked for, and a dead-looking dashboard polling with a maybe-valid
      // cookie would be worse than the honest login screen plus the warning.
      .finally(() => setUser(null));

  const identify = async (slot: number, on: boolean, duration: IdentDuration) => {
    if (!selectedEnclosure) return;
    setBusy(true);
    setError(null);
    try {
      await api.identify(selectedEnclosure, slot, on, duration);
      await refreshBays();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!ready) return <div className="login-wrap"><div className="panel">Loading…</div></div>;
  // With authentication disabled there is no account to sign in to, so
  // demanding one would gate nothing and only block the dashboard.
  if (!user && authRequired)
    return (
      <LoginScreen
        needsBootstrap={needsBootstrap}
        notice={signOutNotice}
        onAuthenticated={(u) => {
          setUser(u);
          setNeedsBootstrap(false);
          setSignOutNotice(null);
        }}
      />
    );

  const enclosure = enclosures.find((e) => e.logical_id === selectedEnclosure) ?? null;
  const selectedBay = bays.find((b) => b.ses_slot === selectedSlot) ?? null;
  // Only the map's marker is put out locally. The detail panel keeps the
  // server's word - "on (0:00)" until the clear is confirmed - so that if the
  // server's clear is failing and being retried, Clear stays enabled and the
  // panel does not claim a dark LED nobody has verified.
  const mapBays = bays.map((b) => {
    const deadline = identDeadline(b, clockOffsetMs);
    return deadline !== null && deadline <= identNow ? { ...b, locate: false } : b;
  });

  return (
    <div className="app">
      {/* inert while the dialog is up, so Tab cannot leave it for the page behind. */}
      <header className="topbar" inert={changingPassword}>
        <div className="brand">
          KTN Enclosure Manager
          <small>Designed for TrueNAS SCALE</small>
        </div>

        {enclosures.length > 1 && (
          <select
            value={selectedEnclosure ?? ""}
            onChange={(e) => setSelectedEnclosure(e.target.value)}
            aria-label="Enclosure"
          >
            {enclosures.map((e) => (
              <option key={e.logical_id} value={e.logical_id}>
                {e.product} {e.logical_id}
              </option>
            ))}
          </select>
        )}

        <nav className="tabs" role="tablist">
          {(["map", "chassis", "diagnostics"] as Tab[]).map((t) => (
            <button
              key={t}
              role="tab"
              className="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
            >
              {t === "map" ? "Drive map" : t === "chassis" ? "Chassis" : "Diagnostics"}
            </button>
          ))}
        </nav>

        <div className="spacer" />

        {tab === "map" && (
          <input
            type="search"
            placeholder="Search bay, serial, /dev, WWN, pool…"
            aria-label="Search bays"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ minWidth: 240 }}
          />
        )}

        <select
          value={theme}
          onChange={(e) => setTheme(e.target.value as Theme)}
          aria-label="Theme"
        >
          <option value="system">System theme</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>

        {/* "updated <slot poll time>" used to sit here labelling five clocks at
            once - the shelf temperature (two minutes old), the ZFS state (twenty
            seconds) and the SAS address all wore the five-second slot poll's
            timestamp. FreshnessSummary states the oldest of the *stamped*
            sources instead, and expands to the per-source breakdown. Stamped is
            the honest qualifier: /bays publishes no timestamp for the SES/AES
            poll, so the SAS address in the detail panel rides a clock this
            summary cannot see and says so where it renders. */}
        <FreshnessSummary sources={sources} />
        {user ? (
          <>
            <button ref={passwordButton} className="btn secondary"
                    onClick={() => setChangingPassword(true)}>
              Change password
            </button>
            <button className="btn secondary" onClick={() => void signOut()}>
              Sign out ({user})
            </button>
          </>
        ) : (
          <span className="badge warning" title="KTN_AUTH_REQUIRED is false">
            unauthenticated
          </span>
        )}
      </header>

      {changingPassword && (
        <ChangePasswordDialog
          onClose={() => setChangingPassword(false)}
          onChanged={() => {
            // Changing the password bumps the account's session epoch, which
            // invalidates every existing cookie - including this one. Sending
            // the user straight back to the login screen is the honest
            // response; leaving them on a dead session would show confusing
            // 401s on the next poll.
            setChangingPassword(false);
            setUser(null);
            setSignOutNotice("Password changed. Sign in with your new password.");
          }}
          onRevoked={() => {
            // Same epoch bump as a password change, so the same honest flow:
            // this session is dead too; go sign back in.
            setChangingPassword(false);
            setUser(null);
            setSignOutNotice("All sessions signed out. Sign in again.");
          }}
        />
      )}

      <main inert={changingPassword}>
        {error && <div className="notice error" role="alert">{error}</div>}
        {/* First, because it is the more fundamental failure: TrueNAS going away
            costs the pool columns, but the slot poll going away means the map
            itself - presence, health, IDENT, fault - is a photograph. The rows
            keep being served from the last good read (§37), which is right, and
            is exactly why the page has to say so: nothing else about the screen
            changes when the enclosure stops answering. */}
        {sources?.slots_error && (
          <div className="notice warn" data-testid="slots-error">
            Enclosure not readable — the bay map below is the last good snapshot,
            taken{" "}
            {sources.slots
              ? new Date(sources.slots).toLocaleTimeString()
              : "at an unknown time"}
            , and is not refreshing. Drive presence, health, IDENT and fault state
            may have changed since. ({sources.slots_error})
          </div>
        )}
        {sources?.truenas_error && (
          <div className="notice warn">
            TrueNAS unavailable — pool, vdev and SMART data may be stale.{" "}
            {/* The old text asserted unconditionally that "bay state below is
                read directly from the enclosure and remains accurate". That is
                true only while the slot poll is succeeding, and the app now
                knows when it is not; an accuracy claim is worth nothing if it is
                made in the one case where it is false. */}
            {sources.slots_error
              ? "The enclosure is not readable either — see above; nothing on " +
                "this page is currently refreshing."
              : "Bay state below is read directly from the enclosure and is " +
                "still refreshing."}{" "}
            ({sources.truenas_error})
          </div>
        )}
        {enclosures.length === 0 && (
          <div className="notice warn">
            No SES enclosure detected. Check that the HBA is passed through and that
            /sys/class/enclosure is visible to this container.
          </div>
        )}

        {enclosure && tab === "map" && (
          <>
            <EnclosureMap
              enclosure={enclosure}
              bays={mapBays}
              selected={selectedSlot}
              onSelect={(slot) => setSelectedSlot(slot === selectedSlot ? null : slot)}
              query={query}
            />
            {selectedBay ? (
              <BayDetail
                bay={selectedBay}
                sources={sources}
                clockOffsetMs={clockOffsetMs}
                onIdentify={identify}
                busy={busy}
                identDisabledReason={
                  !user && !anonIdentAllowed
                    ? "Identify needs an account. Authentication is disabled on " +
                      "this deployment, so the LED write is refused. Re-enable " +
                      "authentication, or set KTN_ALLOW_ANONYMOUS_IDENT=true."
                    : null
                }
              />
            ) : (
              <div className="panel muted">Select a bay to see disk, ZFS and SMART detail.</div>
            )}
          </>
        )}

        {enclosure && tab === "chassis" && <ChassisView chassis={chassis} />}
        {enclosure && tab === "diagnostics" && (
          <DiagnosticsView enclosureId={enclosure.logical_id} />
        )}
      </main>
    </div>
  );
}
