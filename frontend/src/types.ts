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
  overall: string | null;
  temperature_c: number | null;
  power_on_hours: number | null;
  available: boolean;
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

export interface BaysResponse {
  bays: Bay[];
  sources: {
    slots: string | null;
    truenas: string | null;
    truenas_error: string | null;
    smart: string | null;
  };
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
