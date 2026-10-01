# Provisioning scripts

Scripts that configure an installation through Proskenion's own HTTP API, the
same requests the admin screens make. Nothing here touches the database
directly.

## `stage_lighting.py`

Configures the stage lighting described in `docs/hardware/network_map.md`
("Stage lighting rows"):

| What | Created |
|---|---|
| Fixtures | "Stage 1" … "Stage 15": DMX universe 0, addresses 1–15, on the eDMX8 MAX's `artnet` device, with the existing single-channel dimmer profile (one is created only if no profile has exactly one `dimmer` channel) |
| Bars | Row 1 is the existing "Proscenium" bar (created if absent); "Row 2", "Row 3" and "Row 4 (back)" behind it, front to back; each row's fixtures evenly spaced across its bar |
| Groups | "Stage row 1" … "Stage row 4" (fixtures 1–4, 5–8, 9–12, 13–15), and "Stage all" (1–15), **indicator only**: no fader, never dims anything, so the row faders always work (the Lighting view's Master is the whole-stage fader) |
| Bindings (§8.2 shape B) | one per row, named as the group: KNX `4/0/0`–`4/0/3` → the row at 100 % / 0 %; and four on the panel's "all" switch `4/0/8`, "Stage all → row 1" … "→ row 4", one per row (a binding cannot drive an indicator-only group); fade 0 ms, **disabled** |
| Derived statuses (§8.2 shape C) | "Stage row 1 indicator" … "Stage all indicator": `4/0/4`–`4/0/7`, `4/0/9` = every member at 100 % **as the room sees it** (basis `output`: after group faders and the Master), **disabled** |

Each status address is changed from `incoming` to `both` if it is
`incoming`: a derived status writes its address, and the API refuses one on
an incoming-only address. Nothing is written to the bus while the statuses
are disabled.

It is **idempotent**: it looks everything up first (by name, by group address,
the profile by its shape, the DMX output by host) and creates only what is
missing. A second run prints "Nothing to do". If something exists with a
different configuration, or another fixture is patched inside DMX 1–15, it
lists the **conflicts** and stops before changing anything (exit status 2).
It never renames anything, and the only thing it deletes is the earlier
shape's "Stage all" binding (below).

**Upgrading an installation provisioned before 30 September 2026** (one
"Stage all" binding on `4/0/8` driving an ordinary "Stage all" group,
statuses comparing stored levels): the plan deletes that binding, creates the
four "Stage all → row N" bindings **enabled if it was enabled**, makes "Stage
all" indicator-only, and changes each status to compare what the room sees,
leaving it enabled or disabled as it was. A page button still firing the old
binding is a conflict: re-point it first.

It needs the stage's KNX addresses already in the library, from
`site/knx-commands-both.csv` and `site/knx-feedback-incoming.csv`.

On a fresh installation the bindings and statuses stay disabled until the
swap-over from the legacy controller: enable them in Admin → Rules, the eight
rules on the Rules tab and the five statuses on the Derived status tab.

### Running it on the appliance

**Laptop (Git Bash)**, in the repository: copy the script across.

```bash
ssh admin@10.2.30.251 'rm -rf /tmp/provision && mkdir -p /tmp/provision/tools'
scp -r tools/provision admin@10.2.30.251:/tmp/provision/tools/
```

**CM5 (SSH as admin, bash)**: dry run first. It prints exactly what it would
create or change, and changes nothing.

```bash
cd /tmp/provision
sudo -u auditorium /opt/auditorium/venv/bin/python -m tools.provision.stage_lighting \
    --mint-admin-session --dry-run
```

Check the plan, then **CM5 (SSH as admin, bash)**: the real run.

```bash
cd /tmp/provision
sudo -u auditorium /opt/auditorium/venv/bin/python -m tools.provision.stage_lighting \
    --mint-admin-session
```

**CM5 (SSH as admin, bash)**: run it once more; it should print "Nothing to
do", then tidy up.

```bash
cd /tmp/provision
sudo -u auditorium /opt/auditorium/venv/bin/python -m tools.provision.stage_lighting \
    --mint-admin-session --dry-run
cd / && rm -rf /tmp/provision
```

`--mint-admin-session` runs as `auditorium`, the application's user, because
it reads the application's JWT secret (`/srv/appliance/jwt-secret`, mode
0400) and the admin account's `token_version` from the database, both named
by `/opt/auditorium/config.toml` (`--config` for another). The session it
signs lasts five minutes and is never renewed. Neither the token nor a
password is printed.

**The alternative, signing in with the admin password**, needs no special
user. **CM5 (SSH as admin, bash)**:

```bash
cd /tmp/provision
read -rsp 'Admin password: ' PROSKENION_ADMIN_PASSWORD && echo && export PROSKENION_ADMIN_PASSWORD
/opt/auditorium/venv/bin/python -m tools.provision.stage_lighting \
    --password-env PROSKENION_ADMIN_PASSWORD --dry-run
unset PROSKENION_ADMIN_PASSWORD
```

### Options

| Option | Default | |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8000` | the application, behind nginx |
| `--mint-admin-session` / `--password-env VAR` | one is required | how to sign in |
| `--config` | `$PROSKENION_CONFIG`, then `/opt/auditorium/config.toml` | for `--mint-admin-session` |
| `--dry-run` | off | print the plan; change nothing |
| `--dmx-host` | `10.2.30.245` | finds the `artnet` device by its transport host |
| `--dmx-device-id` | | the `artnet` device by id instead |

Exit status: 0 done or nothing to do; 1 failed part-way (run it again to
finish); 2 conflicts, nothing changed; 3 could not sign in or read the
configuration.

### Tested by

`tests/integration/test_stage_provisioning.py` serves the application under
uvicorn over a database file, with the Art-Net and knxd stubs, and runs the
dry run, the real run and a second run against it, and the upgrade from the
28 September shape the same way.

**Laptop (Git Bash)**, in the repository:

```bash
uv run pytest tests/integration/test_stage_provisioning.py -s
```
