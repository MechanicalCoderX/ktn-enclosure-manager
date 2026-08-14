import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import type { Bay, Chassis, Enclosure, IdentDuration } from "./types";
import { BayDetail, ChassisView, DiagnosticsView, EnclosureMap } from "./views";

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
}: {
  onClose: () => void;
  onChanged: () => void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
         aria-labelledby="cp-title">
      <div className="modal">
        <h2 id="cp-title">Change password</h2>
        <p className="sub">
          Every other session for this account is signed out, so you will be
          asked to sign in again.
        </p>
        <form onSubmit={submit}>
          <label htmlFor="cp-current">Current password</label>
          <input id="cp-current" type="password" autoComplete="current-password"
                 value={current} onChange={(e) => setCurrent(e.target.value)}
                 required autoFocus />

          <label htmlFor="cp-new">New password</label>
          <input id="cp-new" type="password" autoComplete="new-password"
                 minLength={12} value={next}
                 onChange={(e) => setNext(e.target.value)} required />

          <label htmlFor="cp-confirm">Confirm new password</label>
          <input id="cp-confirm" type="password" autoComplete="new-password"
                 minLength={12} value={confirm}
                 onChange={(e) => setConfirm(e.target.value)} required />

          {error && <div className="notice error">{error}</div>}

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
        {error && <div className="notice error">{error}</div>}
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
  const [truenasError, setTruenasError] = useState<string | null>(null);
  const [updated, setUpdated] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [authRequired, setAuthRequired] = useState(true);
  const [anonIdentAllowed, setAnonIdentAllowed] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);
  const [passwordChanged, setPasswordChanged] = useState(false);
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
      setBays(response.bays);
      setTruenasError(response.sources.truenas_error);
      setUpdated(response.sources.slots);
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

  const loadChassis = useCallback(() => {
    if (!selectedEnclosure) return;
    void api.chassis(selectedEnclosure).then(setChassis).catch(() => undefined);
  }, [selectedEnclosure]);

  usePolling(
    loadChassis,
    POLL_CHASSIS_MS,
    Boolean(user && selectedEnclosure && tab === "chassis"),
  );

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
        notice={
          passwordChanged
            ? "Password changed. Sign in with your new password."
            : null
        }
        onAuthenticated={(u) => {
          setUser(u);
          setNeedsBootstrap(false);
          setPasswordChanged(false);
        }}
      />
    );

  const enclosure = enclosures.find((e) => e.logical_id === selectedEnclosure) ?? null;
  const selectedBay = bays.find((b) => b.ses_slot === selectedSlot) ?? null;

  return (
    <div className="app">
      <header className="topbar">
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

        <span className="stamp">updated {updated ? new Date(updated).toLocaleTimeString() : "—"}</span>
        {user ? (
          <>
            <button className="btn secondary" onClick={() => setChangingPassword(true)}>
              Change password
            </button>
            <button
              className="btn secondary"
              onClick={() => api.logout().then(() => setUser(null))}
            >
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
            setPasswordChanged(true);
          }}
        />
      )}

      <main>
        {error && <div className="notice error">{error}</div>}
        {truenasError && (
          <div className="notice warn">
            TrueNAS unavailable — pool, vdev and SMART data may be stale. Bay state below is
            read directly from the enclosure and remains accurate. ({truenasError})
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
              bays={bays}
              selected={selectedSlot}
              onSelect={(slot) => setSelectedSlot(slot === selectedSlot ? null : slot)}
              query={query}
            />
            {selectedBay ? (
              <BayDetail
                bay={selectedBay}
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
