/* Backup (spec §21.24, §13). */
import type { HelpEntry } from "../types";

export const backup = {
  "backup.run-now": {
    term: "Back up now",
    body: "Runs a backup immediately, rather than waiting for the nightly schedule.",
  },
  "backup.baseline.capture": {
    term: "Capture new baseline",
    body: "Records the current configuration as the reference every future backup is compared against.",
  },
  "backup.destination.protocol": {
    term: "Protocol",
    body: "How this destination is reached.",
  },
  "backup.destination.host": {
    term: "Host",
    body: "The destination server's address.",
  },
  "backup.destination.port": {
    term: "Port",
    body: "The destination server's port.",
  },
  "backup.destination.path": {
    term: "Share or path",
    body: "Where on the destination backups are written.",
  },
  "backup.destination.username": {
    term: "Username",
    body: "The account backups are written as.",
  },
  "backup.destination.password": {
    term: "Password",
    body: "Leave blank to keep the stored password unchanged.",
  },
  "backup.destination.save": {
    term: "Save",
    body: "Saves this backup destination.",
  },
  "backup.restore.sha256": {
    term: "SHA-256 (optional, from the .sha256 sidecar)",
    body: "Checked against the archive before anything is restored, if given.",
  },
  "backup.restore.confirm": {
    term: "Restore from this backup",
    body: "Replaces the database, venue baselines and TLS certificates with what this archive holds, then restarts the appliance. Confirms first, and says exactly what it is about to replace.",
  },
  "backup.snapshot.restore": {
    term: "Restore",
    body: "Replaces the database, venue baselines and TLS certificates with this snapshot, then restarts the appliance. A pre-restore snapshot is taken first, so this is itself reversible.",
  },
  "backup.baseline.restore": {
    term: "Restore",
    body: "Replaces the current configuration with this baseline. A pre-restore snapshot is taken first, so this is itself reversible.",
  },
  "backup.image.restore": {
    term: "Restore",
    body: "Writes this system image into the standby slot and boots it on trial — the running slot is never overwritten while it is running. Only images this machine itself captured can be restored this way.",
  },
  "backup.image.delete": {
    term: "Delete",
    body: "Frees the space this image held. It cannot be undone.",
  },
} as const satisfies Record<string, HelpEntry>;
