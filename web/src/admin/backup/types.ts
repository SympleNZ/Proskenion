/*
 * `/system/backup*`, `/system/baseline*` and `/system/images*` wire types
 * (phase-6-contracts.md §5, §6, §8; `proskenion/api/backup.py`,
 * `proskenion/api/baseline.py`, `proskenion/api/images.py`).
 *
 * The images shapes were originally built from contracts §5
 * (`GET/POST /system/images*`) and §21.24's wireframe alone, before the
 * images router existed. They have since been checked against it
 * (`proskenion/api/images.py`) and left as they were — `SystemImage` and
 * `ImageRestoreResult` already matched — except for the step-name arrays
 * below, which were corrected to the router's real sequence once it landed.
 */

// -- progress operations (contracts §6; step names copied from the source that
// owns them, the same convention `admin/certs/types.ts`'s `CERT_PROGRESS_STEPS`
// documents — a mismatch would only surface as a wrong live label) ---------------

/** `appliance/bin/auditorium-helper`'s `backup-now` verb, relayed as `backup_run`. */
export const BACKUP_RUN_OPERATION = "backup_run";
export const BACKUP_RUN_STEPS: readonly string[] = [
  "Starting the backup",
  "Running the backup job",
  "Backup job finished",
  "Done",
];

/** `core/backup_restore.py`'s `ARCHIVE_STEPS` — a restore from an uploaded or
 * held archive, told apart from `SNAPSHOT_STEPS` by having a manifest to check. */
export const BACKUP_RESTORE_OPERATION = "backup_restore";
export const BACKUP_RESTORE_ARCHIVE_STEPS: readonly string[] = [
  "Reading the archive",
  "Checking the archive's checksum",
  "Reading the manifest",
  "Checking the database",
  "Testing the migrations",
  "Taking a pre-restore snapshot",
  "Replacing the configuration",
  "Restarting the appliance",
];

/** `core/backup_restore.py`'s `SNAPSHOT_STEPS` — undoing a restore from its own pre-restore snapshot. */
export const BACKUP_RESTORE_SNAPSHOT_STEPS: readonly string[] = [
  "Reading the snapshot",
  "Checking the database",
  "Testing the migrations",
  "Taking a pre-restore snapshot",
  "Replacing the database",
  "Restarting the appliance",
];

/** `core/baseline.py`'s `PROGRESS_STEPS`. */
export const BASELINE_RESTORE_OPERATION = "baseline_restore";
export const BASELINE_RESTORE_STEPS: readonly string[] = [
  "Waiting for scenes to finish",
  "Migrating the baseline forward",
  "Checking the devices the baseline needs",
  "Taking a pre-restore snapshot",
  "Applying the baseline",
  "Rebuilding the live configuration",
];

/** `appliance/bin/auditorium-helper`'s `capture-image` verb (`steps=4`,
 * `do_capture_image`): checking the image signing key, streaming and
 * gzip-compressing the active slot's root partition, capturing its boot
 * tree, then signing and writing the manifest. The USB copy is not one of
 * these four — it happens afterwards, at the application layer
 * (`proskenion.core.images.ImagesService._copy_to_usb`), reusing the same
 * USB destination logic used elsewhere rather than being part of the
 * privileged helper's own steps, so it is not named here; `capturing` in
 * `ImagesCard.tsx` still covers it, since the request the button waits on
 * does not resolve until it (and retention pruning) are done too. */
export const IMAGE_CAPTURE_OPERATION = "image_capture";
export const IMAGE_CAPTURE_STEPS: readonly string[] = [
  "Verifying the image signing key",
  "Streaming and compressing the active slot",
  "Capturing the boot tree",
  "Signing the manifest",
];

/** `image_restore` (contracts §6, `proskenion.core.images.ImagesService.restore`):
 * four steps narrated by the application itself, not relayed
 * straight from the helper — `write-slot` and `stage-slot` are two verbs
 * with their own independent step counts (5 and 2), and forwarding both
 * under one operation would make this panel's step count jump forward and
 * back rather than count up once. Verifying comes first for real, not last:
 * nothing is written to a slot before the image itself has been checked. */
export const IMAGE_RESTORE_OPERATION = "image_restore";
export const IMAGE_RESTORE_STEPS: readonly string[] = [
  "Verifying the image",
  "Writing the image to the standby slot",
  "Staging the slot for trial boot",
  "Rebooting",
];

// -- destinations (contracts §5, §3, §13.3) -------------------------------------

export interface DestinationStatus {
  attempted: boolean;
  ok: boolean | null;
  reason: string | null;
}

export interface LocalDestinationInfo {
  path: string;
  retention_days: number;
}

export interface UsbDestinationInfo {
  path: string;
  retention_days: number;
  present: boolean;
}

export type NetworkProtocol = "smb" | "sftp";

export interface NetworkDestinationInfo {
  protocol: NetworkProtocol | null;
  host: string | null;
  port: number | null;
  path: string | null;
  username: string | null;
  password_set: boolean;
  enabled: boolean;
  retention_days: number;
  updated_at: string | null;
}

export interface BackupDestinations {
  local: LocalDestinationInfo;
  usb: UsbDestinationInfo;
  network: NetworkDestinationInfo;
}

/** `PUT /system/backup/destinations` body — an absent or empty password leaves the stored one unchanged. */
export interface NetworkDestinationUpdate {
  protocol: NetworkProtocol | null;
  host: string | null;
  port: number | null;
  path: string | null;
  username: string | null;
  password?: string | null;
  enabled: boolean;
}

// -- status, history, run and verify (contracts §5, §13.4) -----------------------

export interface BackupRunStatus {
  attempted_at: string;
  source: "scheduled" | "manual";
  archive_id: string | null;
  result: "success" | "failed";
  detail: string | null;
  consecutive_failures: number;
  retried: boolean;
  destinations: Record<string, DestinationStatus>;
}

/** What a monthly check found (§13.4). Only `untrusted` says the archive is bad: `missing` means no
 * destination still holds it, `unreachable` that the one holding it could not be reached. */
export type VerifyOutcome = "verified" | "untrusted" | "missing" | "unreachable" | "none";

export interface BackupVerifyStatus {
  verified_at: string;
  archive_id: string | null;
  ok: boolean;
  detail: string;
  outcome: VerifyOutcome;
  /** Which destination the checked copy was read from, when one was. */
  destination: string | null;
}

export interface NetworkDifference {
  key: string;
  current: unknown;
  archived: unknown;
}

export interface BackupRestoreResult {
  at: string;
  source: "upload" | "local" | "usb" | "network" | "snapshot";
  archive_id: string | null;
  created_at: string | null;
  schema_version: number | null;
  app_version: string | null;
  sha256: string | null;
  checksum_verified: boolean;
  /** The pre-restore snapshot: pass it back as `{snapshot}` to undo this restore (§21.24). */
  snapshot: string;
  baselines_snapshot: string | null;
  replaced: string[];
  not_applied: string[];
  migrations_pending: string[];
  network_differences: NetworkDifference[];
  device_passwords_require_reentry: boolean;
  devices_needing_passwords: string[];
  /** Settings outside the device table whose passwords must be re-entered (email, the NAS). */
  settings_needing_passwords: string[];
  restarted: boolean;
  /**
   * The certificate nginx serves is now a different one. The browser that
   * accepted the old one refuses it until the page is reloaded — and on an
   * address it does not name (`certificate_names`), for good.
   */
  certificate_replaced: boolean;
  certificate_names: string[];
  /** When the appliance first started on the restored database; null in the restore's own response. */
  restarted_at: string | null;
  acknowledged_at: string | null;
}

/** The restore record as `GET /system/backup/status` returns it after the restart. */
export interface LastRestore extends BackupRestoreResult {
  /** Of `devices_needing_passwords`, those still not re-entered on this appliance. */
  devices_still_needing_passwords: string[];
  settings_still_needing_passwords: string[];
}

export interface BackupStatus {
  last_run: BackupRunStatus | null;
  last_verify: BackupVerifyStatus | null;
  /** Shown on the Backup screen until acknowledged or superseded. */
  last_restore: LastRestore | null;
  usb_present: boolean;
  retention_days: Record<string, number>;
}

export interface ArchiveSummary {
  id: string;
  created_at: string;
  source: "scheduled" | "manual";
  size_bytes: number;
  sha256: string;
  schema_version: number;
  app_version: string;
  local_present: boolean;
  usb_present: boolean;
  network_present: boolean;
  verified_at: string | null;
  untrusted: boolean;
  untrusted_reason: string | null;
  /** The backup run's own read-back of the copies it wrote — separate from the monthly check
   * (`verified_at`). `null` for an archive from before that check existed. */
  checked_at: string | null;
  /** The destinations whose copy read back identical, in local, usb, network order. */
  checked_destinations: string[];
}

export interface BackupHistory {
  archives: ArchiveSummary[];
}

/** `POST /system/backup/restore`'s JSON shape — an archive this appliance holds, or a snapshot. */
export interface RestoreFromArchive {
  archive_id: string;
  destination?: "local" | "usb" | "network";
}

export interface RestoreFromSnapshot {
  snapshot: string;
}

/**
 * One pre-change/-restore/-update snapshot (§18 Phase 7, `GET
 * /system/backup/snapshots`) — what its sidecar says, or `null` fields for
 * an older `pre-update-`/`pre-restore-` file from before the sidecar
 * existed (still listed, by name and size).
 */
export interface SnapshotListItem {
  name: string;
  reason: string | null;
  actor: string | null;
  ip_address: string | null;
  taken_at: string | null;
  app_version: string | null;
  size_bytes: number;
  duration_ms: number | null;
}

export interface SnapshotList {
  snapshots: SnapshotListItem[];
}

// -- baseline (contracts §5, §8, §13.5, Q14) --------------------------------------

export interface BaselineCard {
  name: string;
  captured_at: string;
  captured_by: string | null;
  schema_version: string | null;
  app_version: string | null;
  size_bytes: number;
  /** Captured table → row count, for "12 scenes · 16 fixtures · 5 groups" (§21.24). */
  contents: Record<string, number>;
}

export interface BaselineState {
  current: BaselineCard | null;
  copies: BaselineCard[];
}

export interface FieldChange {
  field: string;
  before: unknown;
  after: unknown;
}

export type RowChangeKind = "added" | "removed" | "changed";

export interface RowChange {
  area: string;
  entity: string;
  id: number;
  name: string;
  change: RowChangeKind;
  fields: FieldChange[];
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

export interface AreaDiff {
  area: string;
  changes: RowChange[];
}

export interface BaselineCompare {
  baseline: BaselineCard;
  areas: AreaDiff[];
  /** Migrations applied to the compared copy before diffing (Q14); empty when none. */
  migrated: string[];
  changes: number;
}

export interface BaselineRestoreResult {
  baseline: BaselineCard;
  /** The pre-restore snapshot, so this restore is itself reversible (§13.5). */
  snapshot: string;
  migrated: string[];
  restored: Record<string, number>;
  /** Mixer channels pulled down to a lowered hirer ceiling (§6.7). */
  pulled_down: Record<string, number>;
}

/** The §21.24 areas, in the server's own display order (`core/baseline.py`'s `AREAS`). */
export const BASELINE_AREA_ORDER: readonly string[] = [
  "scenes",
  "lighting",
  "knx",
  "rules",
  "mixer",
  "video",
  "pages",
  "hirer",
];

export const BASELINE_AREA_LABELS: Readonly<Record<string, string>> = {
  scenes: "Scenes",
  lighting: "Lighting",
  knx: "KNX",
  rules: "Rules",
  mixer: "Mixer",
  video: "Video",
  pages: "Pages",
  hirer: "Hirer access",
};

// -- system images (contracts §5, §13.6, Q13 — provisional, see module doc) ------

export interface SystemImage {
  id: string;
  created_at: string;
  size_bytes: number;
  slot: string | null;
  app_version: string | null;
  os_version: string | null;
  local_present: boolean;
  usb_present: boolean;
}

export interface ImagesList {
  images: SystemImage[];
}

export interface ImageRestoreResult {
  image_id: string;
  slot: string | null;
  restarted: boolean;
}

/** `core/backup.py`'s `RETENTION_DAYS` keys, for the destinations card. */
export type RetentionTier = "local" | "usb" | "network";
