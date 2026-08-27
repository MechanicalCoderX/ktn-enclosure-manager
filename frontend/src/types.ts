export type Health = "ok" | "warning" | "failed" | "empty" | "unknown";

export interface DiskIdentity {
  serial: string | null;
  wwn: string | null;
  model: string | null;
  firmware: string | null;
  size_bytes: number | null;
  sas_address: string | null;
  transport: string | null;
  rotational: boolean | null;
}

export interface ZfsInfo {
  pool: string | null;
  vdev: string | null;
  state: string;
  read_errors: number | null;
  write_errors: number | null;
  checksum_errors: number | null;
  is_spare: boolean;
  resilvering: boolean;
}

export interface SmartInfo {
  temperature_c: number | null;
  available: boolean;
  /** TrueNAS has an open DiskTemperatureTooHot alert for this disk. */
  over_temperature: boolean;
  /** The alert text, when there is one. */
  alert: string | null;
}

export interface Bay {
  display_bay: number;
  ses_slot: number;
  enclosure_id: string;
  device: string | null;
  health: Health;
  status: string;
  power_status: string | null;
  locate: boolean;
  fault: boolean;
  ident_expires_at: string | null;
  ident_origin: string | null;
  disk: DiskIdentity;
  zfs: ZfsInfo;
  smart: SmartInfo;
  sysfs_path: string | null;
}

export interface Enclosure {
  logical_id: string;
  vendor: string;
  product: string;
  revision: string;
  scsi_address: string;
  sysfs_path: string;
  sg_device: string | null;
  bsg_device: string | null;
  slot_count: number;
  slots_discovered: number;
}

export interface ChassisElement {
  type_index: number;
  element_index: number;
  subenclosure_id: number;
  element_type: string;
  label: string;
  status: string;
  is_overall: boolean;
  fields: Record<string, string>;
  temperature_c: number | null;
  speed_rpm: number | null;
  /**
   * SES-3 ACTUAL SPEED CODE, the discrete step the enclosure firmware is
   * holding this fan at: 0 = stopped, 1..6 = lowest through second highest,
   * 7 = highest. Null when the shelf printed no speed wording, or wording the
   * backend parser does not map.
   *
   * 0 and null mean OPPOSITE things — a stopped fan is a cooling failure,
   * an unmapped one is merely unknown — so never test this with `!code`,
   * `code ?? 0` or `code || …`. Compare against null explicitly.
   */
  speed_code: number | null;
  /**
   * The speed wording exactly as the shelf printed it, e.g. "Fan at highest
   * speed". Kept verbatim so firmware wording the parser cannot map to a code
   * is still shown rather than lost: this is the safe thing to display,
   * speed_code is the safe thing to compare.
   */
  speed_phrase: string | null;
  /**
   * SES-3 RQSTED ON bit, as reported. Null when the element printed no such
   * field, which is common — absence is not `false` (§13), so this renders as
   * three states, never two. Populated for Power supply elements too, not only
   * Cooling.
   */
  requested_on: boolean | null;
}

export interface Subenclosure {
  subenclosure_id: number;
  vendor: string;
  product: string;
  revision: string;
  logical_id: string | null;
}

export interface Chassis {
  available: boolean;
  error?: string | null;
  enclosure_id?: string;
  subenclosures?: Subenclosure[];
  elements?: ChassisElement[];
  overall_flags?: Record<string, number>;
  collected_at?: string | null;
  stale?: boolean;
}

/**
 * When each class of fact in a Bay was last read. These are four different
 * clocks (§29) and the times genuinely differ by up to two minutes, so they are
 * never collapsed into one stamp — see FreshnessSummary in views.tsx.
 *
 * Every field is nullable in two distinguishable ways, and the difference is
 * the whole point of publishing them:
 *   - a timestamp with no error  → that source is current
 *   - a timestamp with an error  → FROZEN at that time and not refreshing
 *   - null with an error         → configured, but it has never once succeeded
 *   - null with no error         → not reporting at all (typically not
 *     configured, e.g. KTN_TRUENAS_URL unset). Not a fault, and must not be
 *     rendered as one.
 */
export interface BaySources {
  /** Last SUCCESSFUL enclosure slot poll: bay contents, status, IDENT, fault. */
  slots: string | null;
  /**
   * Set while slot polling is failing. `slots` does not advance meanwhile and
   * the bay rows are last-good (§37), so the map is a frozen snapshot — this is
   * the one signal that the enclosure half of the screen cannot be vouched for.
   */
  slots_error: string | null;
  /** Last successful TrueNAS pool/vdev read: everything under ZFS. */
  truenas: string | null;
  truenas_error: string | null;
  /** Last successful SMART temperature read. Much slower than the rest. */
  smart: string | null;
}

export interface BaysResponse {
  bays: Bay[];
  sources: BaySources;
}

export interface AuditEntry {
  timestamp: string;
  user: string;
  enclosure: string;
  bay: number | null;
  ses_slot: number | null;
  serial: string | null;
  operation: string;
  previous: string | null;
  result: string | null;
  verification: string;
  detail: string | null;
}

export type IdentDuration = 10 | 30 | 60 | 300 | null;
