import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "./api";
import type {
  AuditEntry,
  Bay,
  Chassis,
  Enclosure,
  Health,
  IdentDuration,
} from "./types";

/** Health is conveyed by glyph + text + colour, never colour alone (§24). */
const HEALTH: Record<Health, { glyph: string; label: string }> = {
  ok: { glyph: "✓", label: "OK" },
  warning: { glyph: "▲", label: "Warning" },
  failed: { glyph: "✕", label: "Failed" },
  empty: { glyph: "○", label: "Empty" },
  unknown: { glyph: "?", label: "Unknown" },
};

const DURATIONS: { label: string; value: IdentDuration }[] = [
  { label: "10 seconds", value: 10 },
  { label: "30 seconds", value: 30 },
  { label: "60 seconds", value: 60 },
  { label: "5 minutes", value: 300 },
  { label: "Until cleared", value: null },
];

export function formatBytes(bytes: number | null): string {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function stamp(iso: string | null | undefined): string {
  if (!iso) return "never";
  return new Date(iso).toLocaleTimeString();
}

export function HealthBadge({ health }: { health: Health }) {
  const meta = HEALTH[health] ?? HEALTH.unknown;
  return (
    <span className={`badge ${health}`}>
      <span aria-hidden="true">{meta.glyph}</span>
      {meta.label}
    </span>
  );
}

/** Live countdown for a timed IDENT, driven by the server's expiry stamp. */
function Countdown({ expiresAt }: { expiresAt: string }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const remaining = Math.max(0, Math.round((new Date(expiresAt).getTime() - now) / 1000));
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  return <span className="mono">{minutes}:{String(seconds).padStart(2, "0")}</span>;
}

// ----------------------------------------------------------------- shelf map

export function EnclosureMap({
  enclosure,
  bays,
  selected,
  onSelect,
  query,
}: {
  enclosure: Enclosure;
  bays: Bay[];
  selected: number | null;
  onSelect: (slot: number) => void;
  query: string;
}) {
  const needle = query.trim().toLowerCase();
  const matches = (bay: Bay) =>
    !needle ||
    [
      String(bay.display_bay),
      String(bay.ses_slot),
      bay.disk.serial,
      bay.device,
      bay.disk.wwn,
      bay.disk.model,
      bay.zfs.pool,
      bay.zfs.vdev,
    ].some((field) => field?.toLowerCase().includes(needle));

  return (
    <div className="panel">
      <h2>
        {enclosure.vendor} {enclosure.product}
      </h2>
      <div className="sub">
        {bays.length} bays &middot; front view, left to right &middot; Bay 1 = SES slot 0
      </div>
      <div className="shelf-scroll">
        <div className="shelf" role="list" aria-label="Drive bays, left to right">
          {bays.map((bay) => {
            const dim = !matches(bay);
            return (
              <button
                key={bay.ses_slot}
                role="listitem"
                className={`bay ${dim ? "dim" : ""}`}
                aria-pressed={selected === bay.ses_slot}
                aria-label={
                  `Bay ${bay.display_bay}, SES slot ${bay.ses_slot}, ` +
                  `${HEALTH[bay.health]?.label ?? "unknown"}` +
                  (bay.disk.serial ? `, serial ${bay.disk.serial}` : ", empty") +
                  (bay.locate ? ", identify active" : "")
                }
                onClick={() => onSelect(bay.ses_slot)}
              >
                {bay.locate && <span className="ident-dot" aria-hidden="true" />}
                <span className="bay-no">{bay.display_bay}</span>
                <span className="bay-ses">SES {bay.ses_slot}</span>
                <HealthBadge health={bay.health} />
                <span className="bay-serial">{bay.disk.serial ?? "—"}</span>
                <span className="bay-dev">{bay.device ?? ""}</span>
                {bay.smart.temperature_c !== null && (
                  <span className="bay-temp">{bay.smart.temperature_c}&deg;C</span>
                )}
              </button>
            );
          })}
        </div>
      </div>
      <div className="legend">
        {(Object.keys(HEALTH) as Health[]).map((h) => (
          <HealthBadge key={h} health={h} />
        ))}
        <span className="badge ident">
          <span aria-hidden="true">&#9679;</span> Identify active
        </span>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- bay detail

export function BayDetail({
  bay,
  onIdentify,
  busy,
  identDisabledReason,
}: {
  bay: Bay;
  onIdentify: (slot: number, on: boolean, duration: IdentDuration) => void;
  busy: boolean;
  /** Set when Identify is unavailable; explains why, instead of a dead button. */
  identDisabledReason?: string | null;
}) {
  const [duration, setDuration] = useState<IdentDuration>(60);

  return (
    <div className="panel" data-testid="bay-detail">
      <h2>
        Bay {bay.display_bay} &middot; SES slot {bay.ses_slot}
      </h2>
      <div className="sub">
        <HealthBadge health={bay.health} />{" "}
        {bay.zfs.pool ? `${bay.zfs.pool} / ${bay.zfs.vdev}` : "not a pool member"}
      </div>

      {bay.locate && bay.ident_origin === "external" && (
        <div className="notice warn">
          IDENT active &mdash; external/unknown origin. This was not started by this
          application; it is left alone until you clear it.
        </div>
      )}

      <div className="detail-grid">
        <section>
          <h3>Physical</h3>
          <dl className="kv">
            <dt>Bay</dt><dd>{bay.display_bay}</dd>
            <dt>SES slot</dt><dd>{bay.ses_slot}</dd>
            <dt>Status</dt><dd>{bay.status}</dd>
            <dt>Power</dt><dd>{bay.power_status ?? "—"}</dd>
            <dt>IDENT</dt>
            <dd>
              {bay.locate ? "on" : "off"}
              {bay.ident_expires_at && (
                <>
                  {" "}(<Countdown expiresAt={bay.ident_expires_at} />)
                </>
              )}
            </dd>
            <dt>FAULT</dt><dd>{bay.fault ? "set" : "clear"}</dd>
            <dt>sysfs</dt><dd>{bay.sysfs_path ?? "—"}</dd>
          </dl>
        </section>

        <section>
          <h3>Disk</h3>
          <dl className="kv">
            <dt>Device</dt><dd>{bay.device ?? "—"}</dd>
            <dt>Serial</dt><dd>{bay.disk.serial ?? "—"}</dd>
            <dt>Model</dt><dd>{bay.disk.model ?? "—"}</dd>
            <dt>Firmware</dt><dd>{bay.disk.firmware ?? "—"}</dd>
            <dt>Capacity</dt><dd>{formatBytes(bay.disk.size_bytes)}</dd>
            <dt>WWN</dt><dd>{bay.disk.wwn ?? "—"}</dd>
            <dt>SAS addr</dt><dd>{bay.disk.sas_address ?? "—"}</dd>
            <dt>Transport</dt><dd>{bay.disk.transport ?? "—"}</dd>
          </dl>
        </section>

        <section>
          <h3>TrueNAS / ZFS</h3>
          <dl className="kv">
            <dt>Pool</dt><dd>{bay.zfs.pool ?? "—"}</dd>
            <dt>vdev</dt><dd>{bay.zfs.vdev ?? "—"}</dd>
            <dt>State</dt><dd>{bay.zfs.state}</dd>
            <dt>Read err</dt><dd>{bay.zfs.read_errors ?? "—"}</dd>
            <dt>Write err</dt><dd>{bay.zfs.write_errors ?? "—"}</dd>
            <dt>Cksum err</dt><dd>{bay.zfs.checksum_errors ?? "—"}</dd>
            <dt>Resilver</dt><dd>{bay.zfs.resilvering ? "in progress" : "no"}</dd>
            <dt>Spare</dt><dd>{bay.zfs.is_spare ? "yes" : "no"}</dd>
          </dl>
        </section>

        <section>
          <h3>SMART</h3>
          <dl className="kv">
            <dt>Available</dt><dd>{bay.smart.available ? "yes" : "no"}</dd>
            <dt>Temp</dt>
            <dd>
              {bay.smart.temperature_c !== null
                ? `${bay.smart.temperature_c} °C`
                : "unavailable"}
            </dd>
            <dt>Temp alert</dt>
            <dd>
              {bay.smart.over_temperature ? (
                <span className="badge warning">▲ too hot</span>
              ) : (
                "none"
              )}
            </dd>
          </dl>
          {bay.smart.alert && (
            <p className="sub" style={{ marginTop: 8 }}>{bay.smart.alert}</p>
          )}
          {/* Stated rather than left as a bare "—", which reads as broken.
              TrueNAS 25.10 exposes disk temperatures and its own temperature
              alerting, but no SMART attribute endpoint; reading attributes
              directly would mean handing this container every disk device. */}
          <p className="sub" style={{ marginTop: 8 }}>
            Overall SMART status and power-on hours are not exposed by the
            TrueNAS API. Reading them directly would require giving this
            container access to every disk device, which it deliberately does
            not have. Temperature and TrueNAS' own over-temperature alert are
            shown above.
          </p>
        </section>
      </div>

      {identDisabledReason ? (
        // Say why rather than presenting a button that answers 403. The
        // server refuses this regardless; the UI only has to be honest.
        <div className="notice warn" style={{ marginTop: 16 }}>
          {identDisabledReason}
        </div>
      ) : (
        <div className="ident-controls">
          <label htmlFor="ident-duration" className="muted">Identify for</label>
          <select
            id="ident-duration"
            value={duration === null ? "null" : String(duration)}
            onChange={(e) =>
              setDuration(
                e.target.value === "null" ? null : (Number(e.target.value) as IdentDuration),
              )
            }
          >
            {DURATIONS.map((d) => (
              <option key={String(d.value)} value={d.value === null ? "null" : String(d.value)}>
                {d.label}
              </option>
            ))}
          </select>
          <button
            className="btn"
            disabled={busy || !bay.device}
            onClick={() => onIdentify(bay.ses_slot, true, duration)}
          >
            Identify
          </button>
          <button
            className="btn secondary"
            disabled={busy || !bay.locate}
            onClick={() => onIdentify(bay.ses_slot, false, null)}
          >
            Clear
          </button>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ chassis

export function ChassisView({ chassis }: { chassis: Chassis | null }) {
  if (!chassis) return <div className="panel">Loading chassis telemetry&hellip;</div>;
  if (!chassis.available) {
    return (
      <div className="panel">
        <h2>Chassis</h2>
        <div className="notice warn">{chassis.error ?? "Chassis telemetry unavailable."}</div>
      </div>
    );
  }

  // Overall descriptors are SES per-type summaries, not physical sensors, and
  // on this enclosure they contradict the real element readings (e.g. a 30 C
  // "overall" for a group whose only sensor reads 22 C). Showing them would
  // display a temperature no probe ever measured. Keep this filter.
  const elements = (chassis.elements ?? []).filter((e) => !e.is_overall);
  const group = (type: string) => elements.filter((e) => e.element_type === type);
  const subName = (id: number) =>
    chassis.subenclosures?.find((s) => s.subenclosure_id === id)?.product ?? `sub ${id}`;

  const sections: { title: string; types: string[] }[] = [
    { title: "Enclosure / LCC", types: ["Enclosure"] },
    { title: "Controllers", types: ["Enclosure services controller electronics"] },
    { title: "SAS expanders", types: ["SAS expander"] },
    { title: "Power supplies", types: ["Power supply"] },
    { title: "Cooling", types: ["Cooling"] },
    { title: "Temperatures", types: ["Temperature sensor"] },
  ];

  return (
    <>
      <div className="panel">
        <h2>Chassis health</h2>
        <div className="sub">
          Collected {stamp(chassis.collected_at)}
          {chassis.stale && " — stale, last poll failed"}
        </div>
        {chassis.overall_flags && Object.keys(chassis.overall_flags).length > 0 && (
          <div className="table-scroll">
            <table>
              <tbody>
                <tr>
                  {Object.keys(chassis.overall_flags).map((k) => (
                    <th key={k}>{k}</th>
                  ))}
                </tr>
                <tr>
                  {Object.entries(chassis.overall_flags).map(([k, v]) => (
                    <td key={k} className="mono">{v}</td>
                  ))}
                </tr>
              </tbody>
            </table>
          </div>
        )}
        <div className="sub" style={{ marginTop: 12 }}>
          {(chassis.subenclosures ?? []).map((s) => (
            <div key={s.subenclosure_id}>
              <strong>sub {s.subenclosure_id}</strong>: {s.vendor} {s.product} rev {s.revision}
              {s.logical_id && <span className="mono muted"> &middot; {s.logical_id}</span>}
            </div>
          ))}
        </div>
      </div>

      {sections.map(({ title, types }) => {
        const rows = types.flatMap(group);
        if (rows.length === 0) return null;
        return (
          <div className="panel" key={title}>
            <h2>{title}</h2>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Element</th>
                    <th>Subenclosure</th>
                    <th>Status</th>
                    <th>Reading</th>
                    <th>Flags</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((e) => (
                    <tr key={`${e.type_index}-${e.element_index}`}>
                      <td>
                        {e.label}
                        <span className="muted mono"> [{e.type_index},{e.element_index}]</span>
                      </td>
                      <td>{subName(e.subenclosure_id)}</td>
                      <td>
                        <HealthBadge health={e.status === "OK" ? "ok" : "warning"} />
                        <span className="muted"> {e.status}</span>
                      </td>
                      <td className="mono">
                        {e.temperature_c !== null && `${e.temperature_c} °C`}
                        {e.speed_rpm !== null && `${e.speed_rpm} rpm`}
                      </td>
                      <td className="mono muted">
                        {Object.entries(e.fields)
                          .filter(([, v]) => v !== "0")
                          .slice(0, 4)
                          .map(([k, v]) => `${k}=${v}`)
                          .join(", ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}
    </>
  );
}

// -------------------------------------------------------------- diagnostics

export function DiagnosticsView({ enclosureId }: { enclosureId: string }) {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [pages, setPages] = useState<string[]>([]);
  const [page, setPage] = useState("join");
  const [raw, setRaw] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.diagnostics().then(setData).catch((e) => setError(String(e)));
    api.audit(50).then(setAudit).catch(() => undefined);
    api.rawPages().then(setPages).catch(() => undefined);
  }, []);

  const copyable = useMemo(() => JSON.stringify(data, null, 2), [data]);

  return (
    <>
      <div className="panel">
        <h2>Diagnostics</h2>
        <div className="sub">Sanitised &mdash; contains no credentials. Safe to paste into a bug report.</div>
        {error && <div className="notice error">{error}</div>}
        <button
          className="btn secondary"
          onClick={() => navigator.clipboard?.writeText(copyable)}
        >
          Copy diagnostics
        </button>
        <pre className="raw">{copyable}</pre>
      </div>

      <div className="panel">
        <h2>Raw diagnostic pages</h2>
        <div className="sub">
          Predefined read-only operations only; arbitrary sg_ses parameters are not accepted.
        </div>
        <div className="ident-controls">
          <select value={page} onChange={(e) => setPage(e.target.value)}>
            {pages.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
          <button
            className="btn secondary"
            onClick={() =>
              api
                .rawPage(enclosureId, page)
                .then((r) => setRaw(r.output))
                .catch((e: ApiError) => setRaw(`error: ${e.message}`))
            }
          >
            Run
          </button>
        </div>
        {raw && <pre className="raw">{raw}</pre>}
      </div>

      <div className="panel">
        <h2>Audit log</h2>
        <div className="sub">Every write, including automatic IDENT clearing.</div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Time</th><th>User</th><th>Bay</th><th>Serial</th>
                <th>Operation</th><th>Prev</th><th>Result</th><th>Verified</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((e, i) => (
                <tr key={i}>
                  <td className="mono">{new Date(e.timestamp).toLocaleString()}</td>
                  <td>{e.user}</td>
                  <td>{e.bay ?? "—"}</td>
                  <td className="mono">{e.serial ?? "—"}</td>
                  <td>{e.operation}</td>
                  <td className="mono">{e.previous ?? "—"}</td>
                  <td className="mono">{e.result ?? "—"}</td>
                  <td>{e.verification}</td>
                </tr>
              ))}
              {audit.length === 0 && (
                <tr><td colSpan={8} className="muted">No write operations recorded yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
