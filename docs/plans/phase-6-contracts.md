# Phase 6 — contracts

Fixed on 2026-09-20, before wave 1, so the helper, the packaging, the services
and the screens can be built in parallel. **Each task implements exactly this.**
A change needs the coordinator's agreement. The decisions cited as Q1–Q22 are in
`docs/plans/phase-6.md`, all approved.

**General rules:**
- REST paths are under `/api/v1`, and errors use §16.1's closed vocabulary.
- Every list endpoint answers an object wrapping the list.
- Times are ISO 8601 with offset (§4.9). Versions are **directory names**:
  `vX.Y.Z`.
- Nothing the application writes is trusted by anything running as root. The
  helper re-verifies.

## 1. `boot-state.json` — one schema, three writers

`/srv/appliance/boot-state.json` is written by the application, the helper and
`auditorium-update-rollback`.

```json
{
  "active_slot": "a", "last_known_good": "a", "staged": null,
  "slots": {"a": "PARTUUID-of-root-a", "b": "PARTUUID-of-root-b"},
  "started":  {"version": "v1.3.0", "at": "2026-09-20T19:42:11.400+12:00"},
  "healthy":  {"version": "v1.2.0", "at": "2026-09-20T03:10:02.000+12:00"},
  "update":   {"from": "v1.2.0", "to": "v1.3.0", "snapshot": "…", "at": "…"},
  "rollback": {"failed_version": "v1.3.0", "restored_version": "v1.2.0",
               "reason": "…", "at": "…"},
  "trial":    {"slot": "b", "version": "…", "started_at": "…", "deadline_at": "…"}
}
```

**Rules every writer follows:**
- **Read, merge, write.** A writer preserves every key it does not own,
  including unknown ones. The application's dataclass must keep an `extra`
  mapping for anything it doesn't model; a start marker that drops `update` or
  `rollback` is the bug P6-T1 fixes.
- **Versions are directory names** (`v1.3.0`), never `__version__`. The
  application reads the name `/data/app/current` resolves to. A marker whose
  version cannot be resolved that way is written as `null`, never as a guess.
- **`started`** is written at every application start, and **`healthy`** after
  30 healthy seconds.
- **`update`** is written by the updater before the swap, and cleared once the
  new version is healthy.
- **`rollback`** is written by the rollback script, and cleared by the
  application once it has raised the banner and sent the email.
- **`trial`** is written when an OS slot is staged, and cleared on confirm or
  rollback.
- **Writes are atomic:** a temporary file in the same directory, `fsync`, then
  `rename`.

## 2. The privileged helper (Q2)

The application never runs privileged work. It writes a request file, and a
root one-shot unit acts on it.

- **Request:** `/data/run/helper/<uuid>.json`, mode 0600, owned by
  `auditorium`.
- **Unit:** `auditorium-helper@.service`, started by
  `auditorium-helper.path`, running as root, one request at a time.
- **Status:** the helper writes `/data/run/helper/<uuid>.status.json`, which
  the application polls and relays as `progress` frames.

```json
{"verb": "apply-update", "id": "<uuid>", "requested_at": "…",
 "args": {"package": "/data/tmp/upload-<uuid>.tar", "version": "v1.3.0"}}
```

```json
{"id": "<uuid>", "state": "running|done|failed", "step": 2, "of": 5,
 "message": "Verifying signature", "error": null, "finished_at": null}
```

**The verbs, and what each accepts:**

| Verb | Arguments | What the helper does |
|---|---|---|
| `restart-core` | `watchdog_window_s` (optional integer, 1–300) | restarts `auditorium-core.service`, holding §4.7's watchdog drop-in for that long or until the unit is running, whichever is first |
| `reboot` | `mode`: `normal` or `tryboot` | reboots |
| `apply-network` | — | re-renders from `system.json` and reloads |
| `apply-update` | `package`, `version` | **re-verifies**, then extracts, swaps and restarts |
| `write-slot` | `slot`, `image`, `manifest` | **re-verifies**, writes the root image and boot tree |
| `stage-slot` | `slot` | writes `tryboot.txt` and the trial record |
| `confirm-slot` | `slot` | makes the slot permanent |
| `capture-image` | `slot`, `destination` | streams the slot to an image with its manifest |
| `backup-now` | — | runs the backup job |

**The helper's own rules:**
- It validates every argument itself: a slot is `a` or `b` and never the
  active one for `write-slot`; every path is inside `/data/tmp` or
  `/srv/local` and is not a symlink; a version matches `vX.Y.Z`.
- **It re-verifies every package and image against the trust anchors** before
  anything is written outside `/data`. The application's verdict is never
  trusted (Q2, §6.11).
- An unknown verb, a malformed request, or a request older than 10 minutes is
  refused and logged.
- A request file is deleted after it is handled; its status file stays for an
  hour.

## 3. Packages and signing (Q8, Q10, Q13)

**One layout for both package types**, as a tar:

```
manifest.json
manifest.json.sig      Ed25519 over the exact bytes of manifest.json
payload/…              every member listed in the manifest
```

```json
{"type": "app|os", "version": "v1.3.0", "created_at": "…",
 "min_app_version": "v1.1.0",
 "members": [{"path": "payload/app.tar.zst", "sha256": "…", "size": 12345}]}
```

**Verification, in this order, before any extraction:**
1. The signature verifies against an anchor in
   `/usr/local/share/auditorium/trusted-keys/*.pub`. Anchors come only from
   the root image, never from the package.
2. Every member's size and SHA-256 match, hashed while streaming.
3. Nothing outside the manifest exists in the tar.
4. No member is a symlink, hardlink, device, absolute path or `..` path, and
   no path escapes `payload/`.
5. `type` matches the endpoint. An `os` package offered to the app endpoint is
   refused, and the reverse.
6. `version` is higher than the installed one. A downgrade is refused; Roll
   back is the way back.
7. `min_app_version` is satisfied.

**An OS package's payload** (Q10) is `root.img.zst` plus `boot/` — the kernel,
initramfs, `cmdline.txt` and the rest of a `slot-x/` tree. The helper writes
this machine's `root=PARTUUID` and `/etc/fstab` from `boot-state.json`'s
`slots`, and adds `panic=10` and a bounded `rootwait`.

**A system image** (Q13) is the same shape with `type: "image"`, signed by a
key held on `/srv/appliance` and generated on first boot. Only images this
machine captured restore here.

**Signing** never happens in CI (§22.8). `tools/package.py build` produces an
unsigned package, and `sign` is a local step with the private key.

## 4. `system.json` additions (Q22, T12)

```json
{
  "hostname": "auditorium",
  "network": {
    "address": "10.2.30.251/24", "gateway": "10.2.30.254",
    "dns": ["10.2.30.1"],
    "smtp_relay": {"host": "relay.n4l.co.nz", "port": 25},
    "backup_destination": {"address": "…", "protocol": "smb|sftp"}
  }
}
```

- **The relay is a host and a port, never a hard-coded address (Simon,
  2026-09-20).** `auditorium-config-apply` resolves the host, writes one
  outbound rule per resolved address on that port, and re-resolves on each
  apply and nightly. If resolution fails it keeps the last known addresses and
  logs. Port 25 is never opened to every address.
- **The device table is mirrored from the `devices` rows** on every device
  write, so a device at a new address passes the firewall before its test runs
  (the Phase 1 gap).
- Rendering is unchanged otherwise: absent keys keep today's defaults.

## 5. REST — additions to §16.7

All admin unless stated.

| Path | Notes |
|---|---|
| `GET`/`POST /system/network` | `POST` answers `202` with a `confirm_token`; see the flow below |
| `POST /system/network/confirm` | `{confirm_token}` — keeps the new address |
| `GET /system/network/state` | `applied_at`, `reverts_at`, the previous address |
| `GET`/`PUT /system/email`, `POST /system/email/test` | the password is write-only, and never returned |
| `GET`/`PUT /system/certs/token` | write-only; `GET` answers only whether one is set |
| `POST /system/certs/issue`, `GET /system/certs/history` | live steps arrive as `progress` |
| **`GET /system/certs/download`** | **public** (§6.16): the served certificate as `application/x-pem-file` |
| `GET`/`PUT /system/backup/destinations`, `GET /system/backup/status`, `POST /system/backup/run`, `GET /system/backup/history`, `GET /system/backup/{id}/download`, `POST /system/backup/verify` | |
| `GET /system/backup/sftp-key` | the appliance's public key, to install on the NAS |
| `POST /system/backup/restore` | streamed upload, or `{archive_id}` |
| `GET`/`POST /system/baseline`, `GET /system/baseline/compare`, `POST /system/baseline/restore` | |
| `GET /system/images`, `POST /system/images/capture`, `POST /system/images/{id}/restore`, `DELETE /system/images/{id}` | |
| `POST /system/update` | streamed upload; answers the verified manifest for review |
| `POST /system/update/apply` | `{when: "now"|"quiet"}` |
| `POST /system/update/rollback` | |
| `GET /system/os`, `POST /system/os/rollback` | slots, versions, trial state |
| `POST /system/restart`, `POST /system/reboot` | through the helper |

**The network change flow (Q5):**
1. `POST /system/network` validates, answers `202` with a `confirm_token`, and
   asks the helper to apply.
2. The browser is sent to `http://<old-address>/reconnect`, a static page from
   the root image on port 80, which polls `http://<new-address>/health`. CORS
   is allowed on `/health` only.
3. When a Cloudflare token exists, the appliance updates its own DNS A record
   first. Otherwise the page redirects to `https://<new-address>` and says DNS
   needs updating.
4. **Confirm or revert:** without `POST /system/network/confirm` inside 3
   minutes, the helper restores the previous settings.

## 6. Frames

- **`progress`** already exists:
  `{"type": "progress", "operation", "step", "of", "message"}`. The operation
  names are closed: `cert_issue`, `cert_renew`, `backup_run`,
  `backup_verify`, `backup_restore`, `baseline_restore`, `image_capture`,
  `image_restore`, `update_verify`, `update_apply`, `os_write`, `os_stage`,
  `network_apply`.
- **`banner`** keys this phase raises: `backup_failed_amber`,
  `backup_failed_red`, `backup_media_absent`, `backup_untrusted`,
  `update_ready`, `update_rolled_back`, `os_trial`, `cert_expiring`,
  `cert_self_signed`, `email_unconfigured`, `disk_critical`.
- Both are staff-only. A hirer receives neither (Phase 5's filter).

## 7. Alerts (§11.4)

```python
class AlertSink(Protocol):
    async def send(self, kind: str, subject: str, body: str,
                   *, priority: str = "normal") -> None: ...
```

- **Every §11.4 trigger calls it:** the PIN limiter's 10-per-hour alert,
  `RuleAlert`'s `notify`, a device green→red for 60 s, a failed action during
  a hire, backup and media failures, disk critical, and an automatic rollback
  (`priority="high"`).
- **Reconnection sends nothing.** Rate limiting is the limiter's, not the
  sink's.
- The sink reads the live configuration; emergency mode reads
  `smtp-fallback.toml`.
- With no relay configured it logs and raises `email_unconfigured` once.

## 8. Archives and baselines

**An archive** is `auditorium-YYYYMMDD-HHMM.tar.zst`:

```
manifest.json       {created_at, schema_version, app_version, sha256, contents[]}
db/proskenion.db    SQLite online-backup copy
config/…            system.json and smtp-fallback.toml
certs/…             the certificate pair, never the Cloudflare token
baselines/…
```

- It is hashed on creation, and never encrypted (§13.2, B19).
- The Cloudflare token and the device secret are excluded.
- Destination credentials are encrypted with the device secret.
- **Retention:** 14 days local, 7 on USB, 30 on the network.
- **An in-app restore** replaces the database, baselines and certificates
  only. It never applies `system.json` or app code, and keeps the current
  token (Q15). A newer `schema_version` is refused.

**A baseline** is `baselines/current.sqlite` plus dated copies, holding §13.5's
captured tables with the excluded ones emptied. Compare and restore migrate a
copy forward with the startup runner (Q14).

## 9. Bench, and what the contracts assume

- **The relay is reachable only on the school network,** so the bench proves
  email against a local SMTP stub and the real relay is confirmed on site.
- **One SSD, no spare (Q21):** the recovery test runs last, after an image is
  captured to USB. The eMMC rescue image (Q20) is the second boot device, and
  the golden image the fallback.
- **4 GB RAM:** every upload and image stream to `/data/tmp`, never RAM (Q9).

- **`restart-core` takes a bounded watchdog window** (§4.7, §14.5): the request
  says how long, never how wide — the drop-in's contents come from the
  read-only root. It closes as soon as the unit is running. `apply-update`
  still holds its window for the full duration; it is the root-side fallback,
  and the application's apply path uses `restart-core`.

## Additions, 2026-09-20 (wave 3)

- **`POST /system/certs/self-signed`** (admin): generates and serves a
  self-signed pair through the same atomic swap as an issued one, records it in
  the renewal history with its reason, and raises `cert_self_signed`. It is
  §21.24's "Use self-signed" — the way back when issuance cannot work — so it
  depends on neither Cloudflare nor ACME.
- **The confirm token travels in the URL fragment.** `POST /system/network`'s
  token is passed to the appliance's `/reconnect` page and carried to the new
  address in the fragment, never the query string, so it stays out of server
  logs and referrers. The interface reads it on arrival, strips it from the
  URL, and confirms; a token that does not match the pending change is
  refused. The browser's own session storage remains the same-origin fallback.

- **What a captured image carries, for the recovery environment** (P6-T16,
  reading it; P6-T10, writing it):
  - **`payload/partitions.env`** — the partition table's own GUIDs, so a
    recovery recreates the disk with the **recorded PARTUUIDs** and the
    restored root's `cmdline.txt` and `fstab` resolve unchanged. Without it an
    image cannot be restored onto a bare disk, which is the whole point of
    having one.
  - **`image-keys/<id>.pub` beside the image on its medium** — a recovery
    environment has no `/srv/appliance` to read, so the public half of the
    machine's image key travels with the image it verifies.
  The exact shapes are `appliance/recovery/lib/recovery_image.py` and
  `recovery_partitioning.py`.
