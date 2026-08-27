import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "./api";
import type {
  AuditEntry,
  Bay,
  BaySources,
  Chassis,
  ChassisElement,
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

// ------------------------------------------------------------- freshness (§29)

/**
 * How old is what I am looking at?
 *
 * A Bay row is composed from sources on four different clocks: the enclosure's
 * own slot map, TrueNAS pools and vdevs, SMART temperatures, and disk identity
 * read live on every request. One header stamp fed only by the fastest of them
 * used to label all four, so a two-minute-old temperature and a twenty-second-
 * old pool state both sat under "updated <five seconds ago>". That is not a
 * cosmetic inaccuracy - it is why a support timeline could not be
 * reconstructed, because every reading appeared to have been taken at the
 * moment the page claimed.
 *
 * The header therefore states the OLDEST contributing source. "as of T" is then
 * a true statement about every fact on the screen, which is the only thing a
 * single number can honestly be; each panel that actually renders one source
 * carries that source's own stamp (SectionStamp, below), because "how old is
 * this pool state?" is asked while reading the pool state, not while reading
 * the header.
 *
 * Cadences are deliberately NOT printed anywhere here. They are operator-tunable
 * (KTN_POLL_*_SECONDS) and the response carries no copy of them, so a hard-coded
 * "every 20s" would be a fresh instance of exactly the bug being fixed. Only the
 * timestamps the server actually sent are shown.
 */

/** One line of the freshness breakdown. `covers` names what it is responsible for. */
type FreshnessRow = { label: string; at: string | null; error: string | null; covers: string };

function freshnessRows(sources: BaySources): FreshnessRow[] {
  return [
    {
      label: "Enclosure bay map",
      at: sources.slots,
      error: sources.slots_error,
      covers: "bay contents, status, IDENT and fault",
    },
    {
      label: "TrueNAS pools and vdevs",
      at: sources.truenas,
      error: sources.truenas_error,
      covers: "pool, vdev, ZFS state and error counters",
    },
    {
      // /bays publishes no error for this source, only its last success, so a
      // failing SMART poll shows up as a stamp that stops advancing rather than
      // as a message. Ageing in place is still an honest signal; inventing a
      // banner from a field the server did not send would not be.
      label: "SMART temperatures",
      at: sources.smart,
      error: null,
      covers: "disk temperature and the over-temperature alert",
    },
  ];
}

/**
 * Age as a human duration. Clamped at zero: a browser clock running ahead of
 * the appliance's must not print a reading from the future.
 *
 * Seconds are kept all the way to two minutes rather than the usual one,
 * because the slowest source on this page polls on roughly that period - so its
 * whole normal range is reported exactly, and "115s ago" never rounds down into
 * a reassuring "1m ago". Above that the minute figure floors, which understates
 * by under a minute; every caller prints the absolute timestamp beside this, and
 * that value cannot round at all.
 */
function ageLabel(iso: string, now: number): string {
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 120) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m ago`;
}

/**
 * The header stamp: the oldest source, with the full breakdown one click away.
 *
 * A source that has neither a timestamp nor an error is NOT treated as
 * infinitely old. With KTN_TRUENAS_URL unset that integration is simply off,
 * its cache is never polled, and every ZFS and SMART field already renders as
 * "-". Folding that into the summary would age the whole header to "never" and
 * manufacture a fault on a perfectly healthy deployment (§13: absence of data
 * must not be treated as data). Such a source is listed as "not reporting" and
 * excluded from the oldest-of.
 *
 * A source that IS failing needs no special arithmetic: its timestamp freezes
 * while the rest keep advancing, so it becomes the oldest on its own and drags
 * the header back with it. The badge only says why.
 */
export function FreshnessSummary({ sources }: { sources: BaySources | null }) {
  // No interval of its own: App re-renders on every bay poll, which is the
  // fastest clock on the page, so the ages here can never be staler than the
  // data they describe. When polling stops the ages freeze - which is why every
  // row also prints the absolute time, a value that cannot go stale.
  const now = Date.now();
  const rows = sources ? freshnessRows(sources) : [];
  const oldest = rows
    .map((r) => r.at)
    .filter((at): at is string => Boolean(at))
    .reduce<string | null>(
      (worst, at) =>
        worst === null || new Date(at).getTime() < new Date(worst).getTime() ? at : worst,
      null,
    );
  const degraded = Boolean(sources?.slots_error || sources?.truenas_error);

  return (
    <details data-testid="freshness" style={{ position: "relative" }}>
      <summary
        className="stamp"
        style={{ cursor: "pointer", color: degraded ? "var(--warn)" : undefined }}
        title={
          "The oldest of the stamped sources below - not a claim about every " +
          "reading on this page; see the SAS address note in the breakdown. " +
          "Open for the age of each one."
        }
      >
        as of {oldest ? stamp(oldest) : "—"}
        {/* The badge carries glyph, word and colour together, so the degraded
            state survives a monochrome or colour-blind reading (§24). */}
        {degraded && <span className="badge warning" style={{ marginLeft: 6 }}>▲ degraded</span>}
      </summary>

      <div
        role="group"
        aria-label="Data freshness by source"
        style={{
          position: "absolute",
          right: 0,
          top: "calc(100% + 8px)",
          zIndex: 30,
          width: "max-content",
          maxWidth: "min(360px, calc(100vw - 32px))",
          background: "var(--panel)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          padding: "12px 14px",
          boxShadow: "0 8px 24px rgb(0 0 0 / 22%)",
          textAlign: "left",
        }}
      >
        <div className="sub" style={{ marginBottom: 10 }}>
          The bay map is composed from sources read on different clocks. The
          header shows the oldest of the sources listed below - true of the
          map tab, but not a guarantee about every reading on this page (the
          SAS address, below, rides a separate clock and has no row here).
        </div>
        {rows.map((row) => (
          <div key={row.label} style={{ marginBottom: 10 }}>
            <div style={{ fontWeight: 600 }}>{row.label}</div>
            <div className="stamp mono">
              {row.error
                ? `frozen at ${stamp(row.at)} — not refreshing`
                : row.at
                  ? `${stamp(row.at)} · ${ageLabel(row.at, now)}`
                  : "not reporting"}
            </div>
            <div className="stamp">{row.covers}</div>
            {row.error && <div className="stamp" style={{ color: "var(--warn)" }}>{row.error}</div>}
          </div>
        ))}
        <div className="stamp" style={{ borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          Disk identity — serial, model, WWN, capacity — is revalidated
          against the drive's WWN on every request, so a stale cache entry
          cannot outlive a swap. The SAS address is the exception: it comes
          from the chassis poll, whose own stamp is on the Chassis tab.
        </div>
      </div>
    </details>
  );
}

/**
 * The stamp for one block of the bay detail, dating that block to the clock its
 * data actually came from. Placed under the heading it belongs to: the question
 * "how old is this?" is asked while reading the value, and by then the header
 * is off-screen anyway.
 */
function SectionStamp({ at, error }: { at: string | null; error: string | null }) {
  if (error) {
    return (
      <div className="stamp" style={{ marginBottom: 8, color: "var(--warn)" }}>
        <span aria-hidden="true">▲ </span>
        not refreshing — last good {stamp(at)}
      </div>
    );
  }
  // "not reporting" rather than "never": with the source switched off there is
  // nothing to be stale, and the fields below already read "-".
  return (
    <div className="stamp" style={{ marginBottom: 8 }}>
      {at ? `as of ${stamp(at)}` : "not reporting"}
    </div>
  );
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

/**
 * Map a chassis element's SES status onto the shared Health scale, so a
 * Critical power supply reads "failed" and not the same yellow triangle as a
 * merely warned fan. The string arrives verbatim from sg_ses (SES-4 element
 * status codes: OK, Critical, Noncritical, Unrecoverable, Not installed,
 * Unknown, Unsupported, Not available, No access allowed), so match
 * case-insensitively and share the vocabulary of classify() in
 * backend/ktnmgr/services/state.py. Two deliberate choices: SES "Unknown"
 * means the enclosure could not report the element's state — that is worth a
 * look, so it is a warning, while a string this table has never seen falls to
 * the honest "?" instead of pretending a verdict; and "Not installed" is
 * empty, exactly as an unpopulated drive bay is (bays show the ○ badge rather
 * than hiding the slot, and so do these rows — an absent PSU is information).
 */
function sesHealth(status: string): Health {
  switch (status.trim().toLowerCase()) {
    case "ok":
      return "ok";
    case "critical":
    case "unrecoverable":
      return "failed";
    case "noncritical":
    case "non-critical":
    case "warning":
      return "warning";
    case "unknown":
      return "warning";
    case "not installed":
      return "empty";
    default:
      return "unknown";
  }
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
  sources,
  onIdentify,
  busy,
  identDisabledReason,
}: {
  bay: Bay;
  /**
   * Freshness of each source feeding this bay. Passed in rather than derived,
   * because the four blocks below are read on four clocks and each one has to
   * be able to say which of them it is on.
   */
  sources: BaySources | null;
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
        <section data-testid="section-physical">
          <h3>Physical</h3>
          <SectionStamp at={sources?.slots ?? null} error={sources?.slots_error ?? null} />
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

        <section data-testid="section-disk">
          <h3>Disk</h3>
          {/* Not a SectionStamp: these fields are on no cache at all. sysfs is
              re-read on every request and the WWN re-validated, so the identity
              is live. What is up to one slot-poll old is which /dev node the
              bay holds - so that half gets the slot stamp, and the SAS address,
              which arrives on the chassis poll instead, says so itself. */}
          <div className="stamp" style={{ marginBottom: 8 }}>
            read live · bay-to-device mapping{" "}
            {sources?.slots ? `as of ${stamp(sources.slots)}` : "not reporting"}
          </div>
          <dl className="kv">
            <dt>Device</dt><dd>{bay.device ?? "—"}</dd>
            <dt>Serial</dt><dd>{bay.disk.serial ?? "—"}</dd>
            <dt>Model</dt><dd>{bay.disk.model ?? "—"}</dd>
            <dt>Firmware</dt><dd>{bay.disk.firmware ?? "—"}</dd>
            <dt>Capacity</dt><dd>{formatBytes(bay.disk.size_bytes)}</dd>
            <dt>WWN</dt><dd>{bay.disk.wwn ?? "—"}</dd>
            <dt title="Read on the chassis poll, not with the rest of this block">
              SAS addr
            </dt>
            <dd>
              {bay.disk.sas_address ?? "—"}
              <span className="stamp"> · from the chassis poll</span>
            </dd>
            <dt>Transport</dt><dd>{bay.disk.transport ?? "—"}</dd>
          </dl>
        </section>

        <section data-testid="section-zfs">
          <h3>TrueNAS / ZFS</h3>
          <SectionStamp at={sources?.truenas ?? null} error={sources?.truenas_error ?? null} />
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

        <section data-testid="section-smart">
          <h3>SMART</h3>
          {/* The slowest source on the page by a wide margin. Under the old
              single header stamp a reading this old was labelled with the slot
              poll's time, which is what made a temperature timeline
              unreconstructable. */}
          <SectionStamp at={sources?.smart ?? null} error={null} />
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

/**
 * SES-3 numbers the actual-speed steps 0..7, where 0 is "the fan has stopped"
 * and 7 is the highest step. The meter therefore has seven segments and fills
 * `code` of them: a stopped fan shows an empty meter, a fan at 7 a full one.
 */
const SPEED_STEPS = 7;

/**
 * The speed step the enclosure firmware has chosen, as a meter plus the number
 * plus the shelf's own wording.
 *
 * rpm and step are independent facts and neither derives from the other - rpm
 * is a free-running measurement, the step is the discrete setting firmware is
 * holding - which is why both columns exist.
 *
 * `code === 0` and `code === null` are opposite statements: a stopped fan is a
 * cooling failure, an unmapped one is merely unreadable wording. Every test
 * here is against null explicitly for that reason; `!code` or `code ?? 0` would
 * silently report an alarm the shelf never raised.
 */
function SpeedStep({ code, phrase }: { code: number | null; phrase: string | null }) {
  // Early return rather than a boolean flag, so the meter below is written
  // against a `code` TypeScript has genuinely narrowed to a number. The phrase
  // is still shown when the code is absent: unmapped firmware wording is worth
  // reading even when nothing can be compared against it.
  if (code === null || code === undefined) {
    return (
      <>
        <span className="mono">—</span>
        {phrase && <div className="stamp">{phrase}</div>}
      </>
    );
  }
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        {/* Decorative: the number and the shelf's own wording beside it carry
            the meaning, so nothing is conveyed by the bar alone (§24). */}
        <span aria-hidden="true" style={{ display: "inline-flex", gap: 2 }}>
          {Array.from({ length: SPEED_STEPS }, (_, i) => (
            <span
              key={i}
              style={{
                width: 5,
                height: 12,
                borderRadius: 1,
                background: i < code ? "var(--accent)" : "var(--border)",
              }}
            />
          ))}
        </span>
        <span className="mono" style={code === 0 ? { color: "var(--warn)" } : undefined}>
          {code} of {SPEED_STEPS}
        </span>
      </div>
      {phrase && <div className="stamp">{phrase}</div>}
    </>
  );
}

/** The generic element table: one row per element, one reading column. */
function ElementTable({
  rows,
  subName,
}: {
  rows: ChassisElement[];
  subName: (id: number) => string;
}) {
  return (
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
                <HealthBadge health={sesHealth(e.status)} />
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
  );
}

/**
 * Cooling elements, which carry three facts the generic table cannot show.
 *
 * This shelf's firmware modulates its fan banks on its own and never tells the
 * host it has done so. rpm was the only one of the three the app rendered, and
 * it is the least interpretable: it moves continuously, so nothing about a
 * reading of 4600 says whether the enclosure is holding a middle step or on its
 * way somewhere. The step code does say that, and it is what makes a bank at 4
 * next to a bank at 7 legible as the firmware doing something rather than as
 * noise - which is the whole reason for showing any of this.
 *
 * Nothing here is settable. There is no fan control in this application and
 * none is planned: chassis management is read-only (§15), so the panel says so
 * rather than leaving an operator hunting for the control these numbers imply.
 */
function CoolingTable({
  rows,
  subName,
}: {
  rows: ChassisElement[];
  subName: (id: number) => string;
}) {
  // Only steps actually reported. Unmapped wording contributes nothing, rather
  // than collapsing to 0 and inventing a disagreement between the banks. Both
  // absent forms are excluded: an older backend omits the key entirely, and an
  // undefined that slipped through would sort as NaN.
  const steps = [
    ...new Set(
      rows
        .map((e) => e.speed_code)
        .filter((c): c is number => c !== null && c !== undefined),
    ),
  ].sort((a, b) => a - b);

  return (
    <>
      <div className="sub">
        Read-only. This application never writes fan speed and offers no control
        to do so — the enclosure firmware picks the step and these are the values
        it reports back. &ldquo;Requested on&rdquo; is the SES RQSTED ON bit
        exactly as printed: a fan can report it clear while running at full
        speed, and most elements do not print it at all.
      </div>
      {steps.length > 1 && (
        // Only rendered when it is news. All banks agreeing is the normal case
        // and needs no line of its own.
        <div className="notice info">
          The banks are at different speed steps (
          {steps.map((s) => `${s} of ${SPEED_STEPS}`).join(", ")}). The firmware
          modulates them independently.
        </div>
      )}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Fan</th>
              <th>Subenclosure</th>
              <th>Status</th>
              <th>Measured</th>
              <th>Step</th>
              <th>Requested on</th>
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
                  <HealthBadge health={sesHealth(e.status)} />
                  <span className="muted"> {e.status}</span>
                </td>
                <td className="mono">
                  {e.speed_rpm !== null ? `${e.speed_rpm} rpm` : "—"}
                </td>
                <td>
                  <SpeedStep code={e.speed_code} phrase={e.speed_phrase} />
                </td>
                <td>
                  {e.requested_on === null || e.requested_on === undefined ? (
                    <span className="muted">not reported</span>
                  ) : e.requested_on ? (
                    "yes"
                  ) : (
                    "no"
                  )}
                </td>
                <td className="mono muted">
                  {Object.entries(e.fields)
                    // Both of these now have a column of their own; repeating
                    // them here in the raw wording would show one fact twice in
                    // two formats and invite the reader to reconcile them.
                    .filter(([k, v]) => v !== "0" && k !== "Actual speed" && k !== "Requested on")
                    .slice(0, 4)
                    .map(([k, v]) => `${k}=${v}`)
                    .join(", ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

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

  const sections: { title: string; types: string[]; kind?: "cooling" }[] = [
    { title: "Enclosure / LCC", types: ["Enclosure"] },
    { title: "Controllers", types: ["Enclosure services controller electronics"] },
    { title: "SAS expanders", types: ["SAS expander"] },
    { title: "Power supplies", types: ["Power supply"] },
    // Cooling is the one type whose status descriptor carries more than a single
    // reading, so it gets a renderer of its own rather than three extra columns
    // that would be empty on every other section. Tagged rather than matched on
    // the title, so retitling the panel cannot silently drop the fan fields.
    { title: "Cooling", types: ["Cooling"], kind: "cooling" },
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

      {sections.map(({ title, types, kind }) => {
        const rows = types.flatMap(group);
        if (rows.length === 0) return null;
        // Only the cooling panel is addressable: the generic panels are several
        // and would share one id, which no test could then scope to.
        return (
          <div
            className="panel"
            key={title}
            data-testid={kind === "cooling" ? "chassis-cooling" : undefined}
          >
            <h2>{title}</h2>
            {kind === "cooling" ? (
              <CoolingTable rows={rows} subName={subName} />
            ) : (
              <ElementTable rows={rows} subName={subName} />
            )}
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
