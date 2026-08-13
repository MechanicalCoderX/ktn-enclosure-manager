import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import type { Bay, Chassis, Enclosure, IdentDuration } from "./types";
import { BayDetail, ChassisView, DiagnosticsView, EnclosureMap } from "./views";

type Tab = "map" | "chassis" | "diagnostics";
type Theme = "system" | "light" | "dark";

const POLL_BAYS_MS = 5000;
const POLL_CHASSIS_MS = 30000;

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
}: {
  needsBootstrap: boolean;
  onAuthenticated: (user: string) => void;
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
  const [theme, setTheme] = useTheme();

  useEffect(() => {
    api
      .authStatus()
      .then((s) => {
        setNeedsBootstrap(s.needs_bootstrap);
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

  useEffect(() => {
    if (!user || !selectedEnclosure) return;
    void refreshBays();
    const timer = setInterval(refreshBays, POLL_BAYS_MS);
    return () => clearInterval(timer);
  }, [user, selectedEnclosure, refreshBays]);

  useEffect(() => {
    if (!user || !selectedEnclosure || tab !== "chassis") return;
    const load = () =>
      api.chassis(selectedEnclosure).then(setChassis).catch(() => undefined);
    void load();
    const timer = setInterval(load, POLL_CHASSIS_MS);
    return () => clearInterval(timer);
  }, [user, selectedEnclosure, tab]);

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
  if (!user)
    return (
      <LoginScreen
        needsBootstrap={needsBootstrap}
        onAuthenticated={(u) => {
          setUser(u);
          setNeedsBootstrap(false);
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
        <button
          className="btn secondary"
          onClick={() => api.logout().then(() => setUser(null))}
        >
          Sign out ({user})
        </button>
      </header>

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
              <BayDetail bay={selectedBay} onIdentify={identify} busy={busy} />
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
