> For: whoever is on hand when the controller stops answering.

*Print this page and laminate it. Keep it inside the rack door, with the
network map.*

## Is it actually down?

Try `https://auditorium.obhs.school.nz/` from a laptop on the auditorium
network. If that fails, try the IP address directly (see the network map
card). If neither answers, it's down.

## What happens by itself — no action needed

- **A bad application update rolls itself back.** Three failed starts inside
  three minutes and the previous version comes back automatically, with an
  email and a banner explaining what happened. Nobody has to do anything.
- **A short power cut is fine.** The clock has a battery, the disk survives,
  and the appliance comes back up on its own.

## If the appliance boots but `/data` is corrupted, disk otherwise healthy

Restore from the last backup through the admin Backup screen — no recovery
stick needed. About 20 minutes. Full steps: `docs/hardware/recovery.md`.

## If the SSD (or the whole board) has failed

1. Get the recovery USB stick from the rack (or, if it's missing, the CM5's
   built-in eMMC already carries the same recovery tool and boots on its
   own once the SSD is gone).
2. Boot from it — an HDMI screen and keyboard, or a browser on the same
   network, walk you through partitioning a replacement disk and restoring
   the last backup onto it.
3. There is no cold spare SSD kept on site — any ordinary M.2 NVMe drive
   works; the recovery tool partitions and images it from scratch. Budget
   time for a trip to buy one first.

Full steps, including the console menu and the web interface's four screens:
`docs/hardware/recovery.md`.

## What a full rebuild looks like (rehearsed 25 September 2026)

This is what actually happened, on the real hardware, end to end:

1. Take a fresh backup, if the controller can still be reached.
2. Build and write a fresh system image.
3. Install the application package.
4. Run the first-run setup wizard.
5. Restore the data backup by uploading it on the Backup screen.
6. Re-enter two things by hand — **they are never in the backup, on
   purpose**: the projector's password, and the Cloudflare API token (for
   the certificate). Everything else — devices, users, scenes, pages, mixer
   setup — comes back from the restore.

**Total human time: about 20 minutes**, not counting how long the image
build itself takes to run unattended.

## Things that catch people out

- **Browse by IP address, not the hostname, until you've got a proper
  certificate again.** Once a browser has ever loaded the site securely, it
  refuses to load it insecurely again on that same hostname — there's no
  "proceed anyway" link, it just fails. The IP address doesn't have that
  problem, because the browser has never loaded it that way before.
- After restoring, the Backup screen shows a "Last restore" note — read it,
  it lists anything still outstanding (like the projector password), and
  dismiss it once you've dealt with them.
- **Device passwords always need re-entering after a restore.** This is
  deliberate, not a bug — the secret that unlocks them never leaves the
  machine, on purpose, so it can never end up in a backup file sitting on
  the school's file server.
- `knxd.conf` (the KNX gateway's own configuration) is *not* in the backup,
  by decision — the gateway's address isn't expected to change, and the
  factory image already has the right one in it.
- After the very first install on a brand new image, the "not installed
  yet" screen may need a reboot to hand over to the real application — this
  becomes automatic once the current image build reaches the appliance;
  until then, if it's still showing that screen a minute or two after
  install finishes, reboot it.

## Who to call

Contact list: `HANDOVER.md` and the card next to this one in the rack.

Full technical procedure: `docs/hardware/recovery.md`. Day-to-day running:
`operator-quick-reference.md`. Handing the hall to a hirer:
`hire-handover.md`.
