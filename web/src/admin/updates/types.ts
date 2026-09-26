/*
 * `/system/update*`, `/system/os*`, `/system/restart` and `/system/reboot`
 * wire types (phase-6-contracts.md §3, §5, §6; `proskenion/core/packages.py`,
 * `proskenion/core/update.py`, `proskenion/core/update_service.py`,
 * `proskenion/core/osupgrade.py`, `proskenion/api/update.py`,
 * `proskenion/api/os_upgrade.py`).
 */

export type PackageKind = "app" | "os";

/** The verified manifest (contracts §3, `manifest_json`): what §21.24 reviews. */
export interface UpdateManifest {
  type: PackageKind;
  version: string;
  created_at: string;
  min_app_version: string | null;
  description: string | null;
  changes: readonly string[];
  key_id: string;
  /** Member count, not the members themselves — the review card names files, not bytes. */
  members: number;
  payload_bytes: number;
}

export interface UploadResult {
  manifest: UpdateManifest;
  sha256: string;
  size: number;
}

export interface DiscardResult {
  discarded: boolean;
}

/** Q17's four conditions, and whether all of them hold right now. */
export interface QuietReport {
  hirer_access_disabled: boolean;
  no_scene_running: boolean;
  no_recent_connection: boolean;
  outside_nightly_window: boolean;
  quiet: boolean;
}

/** In `QUIET_CONDITIONS` order (`core/update.py`) — the order §21.24 lists them. */
export const QUIET_CONDITION_KEYS = [
  "hirer_access_disabled",
  "no_scene_running",
  "no_recent_connection",
  "outside_nightly_window",
] as const;

export type QuietConditionKey = (typeof QUIET_CONDITION_KEYS)[number];

export const QUIET_CONDITION_LABELS: Readonly<Record<QuietConditionKey, string>> = {
  hirer_access_disabled: "Hirer access is off",
  no_scene_running: "No scene is running",
  no_recent_connection: "Nobody has been connected for ten minutes",
  outside_nightly_window: "Outside 02:30–03:30",
};

export interface PendingUpdateInfo {
  version: string;
  sha256: string;
  size: number;
  received_at: string;
  manifest: UpdateManifest;
  prepared: boolean;
}

export interface UpdateHistoryEntry {
  action: "applied" | "rolled_back";
  from: string | null;
  to: string;
  at: string;
}

/** What an automatic or manual rollback did — the detail behind the fixed banner text (§14.5). */
export interface RolledBackInfo {
  from_version: string;
  to_version: string;
  snapshot: string | null;
  at: string;
}

export type UpdateApplyState = "idle" | "preparing" | "waiting_for_quiet" | "applying" | "failed";

export interface UpdateStatus {
  installed_version: string | null;
  state: UpdateApplyState;
  error: string | null;
  rule: string | null;
  pending: PendingUpdateInfo | null;
  quiet: QuietReport;
  previous_versions: readonly string[];
  history: readonly UpdateHistoryEntry[];
  rolled_back: RolledBackInfo | null;
}

export interface AppliedUpdateInfo {
  from_version: string | null;
  to_version: string;
  snapshot: string | null;
  at: string;
}

export type Slot = "a" | "b";

export interface TrialInfo {
  slot: Slot;
  version: string | null;
  started_at: string | null;
  deadline_at: string | null;
  booted_at: string | null;
  on_trial?: boolean;
  healthy_for_s?: number;
}

export type ApplyOutcomeState = "applied" | "waiting_for_quiet" | "os_trial";

export interface ApplyResult {
  state: ApplyOutcomeState;
  applied: AppliedUpdateInfo | null;
  quiet: QuietReport | null;
  trial: TrialInfo | null;
}

export interface RollbackResult {
  from_version: string;
  to_version: string;
  snapshot: string | null;
}

export interface OsPendingInfo {
  version: string;
  sha256: string;
  size: number;
  received_at: string;
  manifest: UpdateManifest;
}

export interface OsStatus {
  active_slot: Slot | null;
  standby_slot: Slot | null;
  active_version: string | null;
  standby_version: string | null;
  last_known_good: string | null;
  staged: string | null;
  trial: TrialInfo | null;
  pending: OsPendingInfo | null;
}

export interface OsRollbackResult {
  slot: string;
  version: string | null;
  mode: string;
}

/** `202` from `POST /system/restart` — the answer arrives before the restart does. */
export interface RestartResult {
  requested: "restart";
  requested_at: string;
}

/** `202` from `POST /system/reboot` — the answer arrives before the reboot does. */
export interface RebootResult {
  requested: "reboot";
  mode: "normal";
  requested_at: string;
}

/**
 * `UpdateRunner.APPLY_STEPS` (`core/update.py`), generic labels rather than
 * the server's `{version}`-interpolated messages — `ProgressPanel` shows the
 * live `message` beside a step already, so the static label only has to name
 * the step, not repeat what the message says.
 */
export const APPLY_STEPS: readonly string[] = [
  "Extracting the package",
  "Building the Python environment",
  "Testing the database migrations",
  "Capturing a database snapshot",
  "Recording the update",
  "Switching to the new version",
  "Restarting the application",
];

export const APPLY_OPERATION = "update_apply";
export const VERIFY_OPERATION = "update_verify";

/** `core/osupgrade.py`'s `WRITE_OPERATION`/`STAGE_OPERATION` and the helper's own step names. */
export const OS_WRITE_STEPS: readonly string[] = [
  "Verifying the operating system package",
  "Writing the root image",
  "Writing /etc/fstab",
  "Writing the boot tree",
];

export const OS_STAGE_STEPS: readonly string[] = ["Arming one boot into the new slot", "Recording the trial"];

export const OS_WRITE_OPERATION = "os_write";
export const OS_STAGE_OPERATION = "os_stage";
