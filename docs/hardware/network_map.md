# Network and hardware map

The installation's addresses, identifiers and physical locations — what a
maintainer replacing a cable or a device needs to know, and cannot work out
from the code (spec §19.2, §20.1). Keep it current: it is on the laminated
recovery card.

**This file must carry no credentials** (§20.3). Passwords and tokens belong
in `HANDOVER.md`, which is never committed.

Sections marked *to be recorded at commissioning* are filled in on site,
following `docs/hardware/setup.md`.

---

## HDMI matrix — Lenkeng LKV422

| | |
|---|---|
| Signalling level | **True RS-232** |
| Measured | TX against GND, switcher powered, nothing connected: **−9.21 V** |
| Date | 2026-09-11 |
| Measured by | Simon Wright |
| Cable | FTDI USB-RS232-WE-1800-BT_0.0 — 1.8 m, bare tinned wire ends, true RS-232 levels |
| Cable ordered | 2026-09-11 |
| Longer run | USB-RS232-WE-5000-BT_0.0 (5 m) if the controller and switcher are not adjacent |
| Order from | An authorised distributor only — Mouser, DigiKey, element14/Farnell NZ or RS Components NZ |
| Cable serial | `AV0M87RY` (recorded 24–25 September 2026) |
| Device path | `/dev/serial/by-id/usb-FTDI_USB-RS232-WE_AV0M87RY-if00-port0` — on the CM5 over USB, not a network device |

Why it matters (§7.5). The vendor manual says "RS-232 3 Pin" without stating a
voltage, and that phrasing is used for both true RS-232 and TTL-level serial.
The two cable types are not interchangeable: a true RS-232 cable drives ±5 to
±12 V, which would damage a TTL port. A negative idle voltage is only produced
by a true RS-232 driver, so this reading settles it.

Order the `_0.0` variant. The `_3.3` and `_5.0` variants add a powered
conductor at the wire end; the LKV422 has its own supply, and a live wire loose
in a terminal block is a hazard for no benefit.

Buy from an authorised distributor because counterfeit FT232R chips are common,
and the failure that matters is not the chip dying. It is a duplicated or absent
USB serial number, which silently breaks the udev rule of §4.12 and binds the
device path to the wrong thing.

**Replacing the cable** means updating the device path or the udev rule,
because a new cable carries a new serial number (§20.2).

---

## Mixer — Allen & Heath CQ-20B

| | |
|---|---|
| Address | *to be recorded at commissioning* |
| MIDI control | TCP 51325 — one connection, held by the controller |
| Native metering | TCP 51326 — **holds one of the two MixPad slots** |
| MixPad | WiFi or USB only, never Ethernet (§20.2). **One tablet at a time** |

Bench-tested 2026-09-11: with the controller's MIDI and native connections held, one MixPad connected and a second was refused. The native protocol is MixPad's own, so the CQ counts the controller's metering connection against the same two slots; MIDI is a separate limit of one and does not count. See
`docs/protocols/cq20b-native.md` §9.

---

## To be recorded at commissioning

- Controller address, hostname, MAC, and the switch port it is patched to (§3.1)
- KNX gateway address and the knxd tunnel verification (`docs/hardware/setup.md`)
- DMX node (eDMX8 MAX): address, universe assignment, port directions, and the
  booth input configuration
- Projector address and PJLink authentication mode (password in `HANDOVER.md`)
- USB serial numbers for every serial cable, and which physical USB port each
  occupies (§4.12, §17.2)
- Backup media label and capacity
- The physical location of each device in the rack and the booth

---

## Site survey — 21 September 2026

Taken from the school network with the appliance's own probes, before the
appliance itself was installed. Every check below was read-only: no lamp
moved, no fader moved, and nothing was written to the KNX bus.

### Addresses, as found

| Device | Address | How it answered |
|---|---|---|
| KNX IP interface | `10.2.30.252` | KNXnet/IP discovery and a tunnel |
| DMXking eDMX8 MAX | `10.2.30.245` | ICMP only — see below |
| Allen & Heath CQ-20B | `10.2.30.248` | MIDI on 51325, metering on 51326 |
| Panasonic PT-EZ570E | `10.2.30.249` | PJLink on 4352 |
| Older DMXking eDMX4 DIN | `10.2.30.220` | Art-Net; still driving the rig |

**The VLAN itself**, read from a laptop on it: gateway **`10.2.30.254`**,
resolvers **`10.2.40.1`** and **`122.56.237.1`**, mask `/24`. `10.2.30.1`
answers nothing — the plan had assumed it was the router, and an appliance
configured that way would have had no route off the VLAN at all: no time, no
certificate, no mail. `10.2.30.45` and `10.2.30.100` were both free on the
day, which is what the survey probed — those being the addresses the plan
then meant to take.

These are the addresses the devices already had. §3.1 was corrected to them
rather than the devices being re-addressed.

**The controller's own address changed afterwards.** The school assigned it
**`10.2.30.251`** on 22 September 2026, in place of the planned `.45`; §3.1,
`build.sh --address` and the certificate's A record all now say `.251`. Worth
knowing that **`.251` was never probed during the survey** — it was chosen
after we were off the network — so "is anything already answering on it?" is
a commissioning check rather than something this page can vouch for. `.100`
is unchanged: it is the optional Phase 9 control surface, not the controller.

### KNX — Weinzierl KNX IP Interface 732 secure

| | |
|---|---|
| Individual address | 1.1.1 |
| Medium | TP1; programming mode off |
| Serial | `00:c5:01:03:60:c3` |
| MAC | `00:24:6d:02:10:57` |
| Services | Core v2, Device Management v2, **Tunnelling v2 — no Routing** |
| Plain tunnel | **Accepted.** KNX Secure is not enforced |
| Address it assigned us | 1.1.3 |

Two things follow. **Tunnelling is the only way in**, which is what knxd's
`-b ipt:` already does; routing would not have worked. And the installation
is on area **1.1.x**, while `knxd.conf.default` uses `0.0.1` for knxd itself
and `0.0.2` upward for its clients — check that against the installation's
own address plan at commissioning, because those are bus addresses and a
clash is a fault that only shows under load.

### Projector — Panasonic PT-EZ570E

| | |
|---|---|
| PJLink class | **1** (not 2) |
| Firmware | Version 1.01, Network 2.00 |
| Projector name | `AUDITORIUM` |
| Lamp hours | 1172, at survey |
| Error status | `000000` — nothing reported |
| Inputs it offers | `11 12 13 21 22 23 24 25 31 32 33 46 47 48` |
| Authentication | On. The password is in `HANDOVER.md`, never here |
| Idle timeout | **30 s** — it closes a quiet connection itself |
| Fresh connect + auth | **45-90 ms**, measured over ten attempts |
| A second connection | **Hangs.** Not refused — no handshake at all until it times out |

**The 30 s idle timeout is the number the poller had to be checked against.**
The projector drops a connection it has heard nothing on for 30 seconds, so a
poll interval anywhere near it would spend its life reconnecting, and a
connection held open between polls would be found dead at the worst moment.
The driver already opens a connection per command and closes it (§21.14), and
a fresh connect with MD5 authentication costs 45-90 ms, so there is nothing to
gain from holding one open and a timeout to lose by it. No change needed; the
figure is here so the next person does not have to re-measure it to find that
out.

**Class 1 settles a question Phase 3 left open**, and costs less than it
sounds. `%1POWR ?` is a Class 1 query and answers `0`, `1`, `2` or `3` — off,
on, cooling, warming — so the appliance still knows and shows which of those
the projector is in, disabling the buttons with the reason while it
transitions (§21.14, §7.4). What Class 1 does not give is the **seconds
remaining**, which §21.14 already hedges as "where PJLink reports it": the
state line reads "Cooling down" here rather than "Cooling down — 47 s".
Nothing in the code or the interface assumed otherwise.

**A second connection hangs rather than being refused**, which is the more
awkward of the two ways it could have gone. Three connections opened in turn:
the first greeted in 24 ms, the second and third never completed a handshake
and timed out at 5 s, and the first stayed healthy throughout. The driver's
one-connection-at-a-time lock is therefore right, but a *foreign* client
holding the projector — the legacy controller is still in the rack — costs us
a 10 s `CONNECT_TIMEOUT` per command rather than a fast refusal. The 30 s idle
timeout above is what eventually clears it. See `docs/protocols/pjlink.md` §8.

### Mixer — Allen & Heath CQ-20B

| | |
|---|---|
| MIDI control | Answers `get` in 2–11 ms |
| Fader law | **Matches the published table exactly** — 0.0 dB reads 12544, −20.0 dB reads 5952 |
| Pan, hard left / centre / hard right | **0 / 8191 / 16383** |
| Pan centre, written | Write **8192**; it reads back 8191. Writing 8191 lands at 7970 |
| A second MIDI connection | **Accepted, then immediately reset.** The first survives |

The fader law was the largest open question Phase 4 carried, and it is
answered: the desk's "NRPN Fader Law" setting is the one the protocol PDF
documents and the driver assumes, so the dB the appliance shows is the dB the
desk means. Two traps came out of the same session and are worth keeping:

- **MixPad rounds the dB it displays.** A channel displaying "0.0" read
  +0.14 dB and one displaying "−10.0" read −9.57 dB. A law check is only
  meaningful when the value is set deliberately rather than dragged until the
  display looks right, and an apparent constant offset between MixPad and the
  desk is this, not a law difference.
- **Pan is quantised to steps of about 221 counts**, so 8192 and 8191 — one
  count apart — fall either side of a step boundary and land 221 counts
  apart. Writing back the 8191 the desk reports would put every centre left
  of centre. The driver writes 8192 and accepts either on the way back.

All of this changed or confirmed the driver; see `docs/protocols/cq20b.md` §16.

### DMX nodes — read from the VLAN, 21 September 2026

`tools/artnet_probe.py`, run from a laptop on `10.2.30.0/24`. Both nodes
answered. (From any other subnet neither does: a node answers a poll by
**broadcast**, which a router will not carry.)

**DMXking eDMX8 MAX — `10.2.30.245`** — the one this system will drive.

| | |
|---|---|
| Node name | `OBHS eDMX8 MAX` |
| Serial | `001A19440130`  MAC `00:1a:19:44:01:30` |
| Firmware | 4.11 |
| **Inputs** | 2 ports — **universes 0 and 1** |
| **Outputs** | 6 ports — **universes 2, 3, 4, 5, 6 and 7** |

**DMXking eDMX4 DIN — `10.2.30.220`** — the older node, still driving the rig.

| | |
|---|---|
| Node name | `OBHS_DMX` |
| MAC | `00:1a:19:00:d2:0a`  firmware 2.7 |
| Ports | 4: three outputs and one input, **all on universe 0** |

**What this means for the configuration.** The lighting output's universe is
configuration, not an assumption in the code (`proskenion/core/dmx/artnet.py`
takes it as a number), so nothing here is a defect — but it has to be set to
match. As the eDMX8 MAX stands, DMX leaves it on **universes 2 to 7**, and a
frame sent to universe 0 would arrive at a port configured as an **input** and
light nothing. The external-desk handoff of §7.2.7 is the other side of the
same coin: the visiting desk's DMX arrives on **universe 0 or 1**, and that is
what external-control detection must watch.

Two things remain for commissioning, with the integrator:
1. **Which universes the system drives** — the eDMX8's outputs as they stand
   (2–7), or the node repatched to match the old rig's universe 0. Whichever
   is chosen, the old eDMX4 at `.220` is on universe 0 today, so running both
   at once on universe 0 would have two nodes answering for the same
   addresses.
2. **Which input universe is the visiting desk's**, since this node offers
   two.

The old node's address, `10.2.30.220`, is outside §3.1's table. If it stays on
the network after cutover it needs its own firewall row, or the appliance will
not be able to talk to it at all.

### The mail relay

| | |
|---|---|
| Name | `relay.n4l.co.nz`, port 25, no authentication |
| Addresses | `103.96.20.28`, `103.96.22.28` |
| Behind it | Mimecast (`au-smtp-outbound-1.mimecast.com`) |
| Certificate | `*.mimecast.com` only — **`relay.n4l.co.nz` can never validate** |
| Reachable from | The school network only |

A test alert was sent from the appliance's own email code and accepted.
**Configure the host as `au-smtp-outbound-1.mimecast.com`**, which keeps
STARTTLS *and* certificate validation and resolves to the same two addresses.
The alternative — `relay.n4l.co.nz` with TLS off — sends school mail in clear
text across the school network, and is only worth taking if N4L changes what
sits behind the relay and the Mimecast name stops being stable.

### What the VLAN lets out

Checked from a laptop on `10.2.30.0/24`, against every outbound destination
the appliance depends on. All of them work:

| What | Destination | Result |
|---|---|---|
| Time | `pool.ntp.org` and the Debian pool, UDP 123 | Answers |
| Mail | `relay.n4l.co.nz` / Mimecast, TCP 25 | Connects, accepts a message |
| Certificates | Let's Encrypt, Cloudflare's DNS API, TCP 443 | Answers |
| Package updates | `deb.debian.org`, TCP 80/443 | Answers |
| DNS | `10.2.40.1`, `122.56.237.1`, UDP 53 | Answer; no filtering seen |

So the VLAN is not the closed network the plan allowed for, and the firewall's
outbound rules (`appliance/nftables.conf`) do not need widening.

**The two placeholder names are settled** (Simon, 21 September 2026). The
golden image shipped `ntp.school.nz` and `av.school.nz`, neither of which
resolves anywhere; both defaults in `appliance/image/build.sh` are now real:

- **Time: `nz.pool.ntp.org`.** The school has no NTP server of its own, so the
  NZ pool is the preferred server and the generic pools stay as fallbacks. The
  cost is that time comes from the internet: with the school's link down there
  is no source at all, where a server on the VLAN would still have answered.
  The CM5's RTC covers the gap and §4.9's degraded time mode covers the rest,
  so this is a tolerable loss rather than a silent one — and `--ntp` is all it
  takes if the school ever stands one up.
- **Hostname: `auditorium.obhs.school.nz`** — **the A record exists and is
  correct**, created 22 September 2026 and verified by lookup:
  `auditorium.obhs.school.nz` → `10.2.30.251`. That answer is itself the proof
  the proxy is off, because a proxied record returns Cloudflare's edge
  addresses instead — the apex `obhs.school.nz` resolves to `104.21.73.91` and
  `172.67.189.86`, ours to the private address, which only a grey-cloud record
  can do (§3.2).

  **It has to be an A record and not a redirect.** A redirect was tried first
  and reversed. A Cloudflare redirect rule answers with a 301 to another URL,
  so a browser sent to `https://10.2.30.251` is connecting to an *address*,
  and a certificate issued for the *hostname* cannot match it — a full-page
  warning on every device, every time, which is what §6.16 exists to avoid. It
  would also route traffic between two machines on the same VLAN out through
  Cloudflare's edge, so the hostname would stop working whenever the school's
  internet did.

  **Still outstanding:** the API token's scope. DNS-01 issues the certificate
  by writing a TXT record at `_acme-challenge.auditorium.obhs.school.nz`, so
  the token needs Zone → DNS → Edit on `obhs.school.nz` **and nothing
  wider** (§3.2 — the most sensitive credential in the system).

  **Worth considering:** the lookup still goes to Cloudflare's nameservers, so
  on a cold cache the name will not resolve during an internet outage. A local
  override on the school's resolver (`10.2.40.1`) would make the hostname
  entirely self-contained. Belt-and-braces, not required.

The zone here is three labels, `obhs.school.nz`, not two. Worth noting because
naive "registrable domain" logic would ask Cloudflare for `school.nz` and find
nothing: `proskenion/core/cloudflare.py`'s `zone_id_for` instead tries every
suffix against Cloudflare's own zone list, stopping before the bare TLD, so it
finds the right zone without knowing where the boundary is. No change needed.

## Commissioning facts, 24–25 September 2026

- **HDMI matrix cable:** FTDI `USB-RS232-WE`, serial **`AV0M87RY`**, on the CM5.
  The device is configured with
  `/dev/serial/by-id/usb-FTDI_USB-RS232-WE_AV0M87RY-if00-port0`, which is
  keyed to that serial and as stable as the `/dev/hdmi-matrix` udev name in
  `setup.md` §5; the udev rule is therefore optional here.
- **`10.2.30.250` is the legacy controller** — the PC running the original C#
  auditorium software. It stays in service until this system has run reliably;
  the lights are still driven by the old eDMX4 at `.220`, and the eDMX8 has
  nothing downstream until the DMX runs are rewired. During that parallel run
  `.250` polls for Art-Net nodes and `.220` broadcasts its DMX input as
  `ArtDmx` to `10.2.30.255` — traffic this system must not mistake for a
  visiting desk.
- **Mail is plain SMTP to `relay.n4l.co.nz:25`, by decision** (Simon,
  24 September). The path is the school network and N4L's managed connection;
  alerts carry no credentials. Plain is also the only setting that works with
  that name — opportunistic STARTTLS is refused, because the relay's
  certificate is Mimecast's. If mail ever has to be encrypted, use
  `au-smtp-outbound-1.mimecast.com` with STARTTLS, which was verified from the
  appliance the same day.
- **Backup drive:** a 14.5 GB Verbatim Store 'n' Go, ext4 labelled
  `AVC-BACKUP`, prepared on the appliance with `sfdisk`/`mkfs.ext4` (the image
  has no `gdisk`), with its root `chown`ed to `auditorium` — without that the
  backup job cannot write to it (a hand-off defect, carried to wave B).


## KNX group addresses seen on the bus (28 September 2026)

Recorded with `knxtool groupsocketlisten` on the controller while Simon pressed
the wall panel's buttons. The controller's tunnel to the gateway (`10.2.30.252`)
works. This is what the installation *uses*, observed; the ETS project is the
authority, and its export (wave D) supersedes this table.

| Function | Command (sent by the panel, `1.1.27`) | Status (sent by the actuator) |
|---|---|---|
| Stage bank 1 | `4/0/0` (1-bit) | `4/0/4` from `1.1.2` |
| Stage bank 2 | `4/0/1` | `4/0/5` |
| Stage bank 3 | `4/0/2` | `4/0/6` |
| Stage bank 4 | `4/0/3` | `4/0/7` |
| All stage banks | `4/0/8` | `4/0/9`, on only while all four are on |
| House lights on/off | `0/0/1` (1-bit) | `0/0/2` (1-bit) from dimmers `1.1.19` and `1.1.20` |
| House lights brightness | not seen: probably `0/0/3` or `0/0/4` (check ETS) | `0/0/5` (1 byte; `FF` = 100%, sent when the fade-up ended) |

Observations:
- **The dimmers fade by themselves.** The panel sends only on/off; the
  dimmers ramp over about 4 s. "On" status is sent at the start of the ramp,
  "off" at the end.
- `1.1.19` repeats its status several times while ramping; that's harmless.
- **The individual addresses** are: panel `1.1.27`, stage actuator `1.1.2`,
  house dimmers `1.1.19` and `1.1.20`. They're on area 1.1, as the site survey
  found. knxd's own `0.0.x` individual addresses are unrelated to the `0/0/x`
  *group* addresses above.

**The controller writes too (28 Sep, 14:16).** `knxtool groupswrite` through
knxd sent: all on, banks 4, 3, 2 and 1 off, then all on. Actuator `1.1.2`
confirmed every command within the same second. The controller's telegrams
came from knxd's client range (`0.0.2`–`0.0.9`); the gateway accepted them.
**Done 29 Sep:** knxd now runs with `-B single` (see "The stage swap-over"
below), so everything the controller sends leaves as the tunnel's assigned
`1.1.3`, not the `0.0.x` client range.


## eDMX8 MAX ports, and the first DMX to real fixtures (28 September 2026)

**The node's own ArtPollReply** (8 bind indexes, one port each), confirmed in
the DMXking configuration tool. This replaces the site survey's "inputs on 0–1".

| Port | Direction | Art-Net universe | Notes |
|---|---|---|---|
| A | output | 0 | Merge **HTP**, failsafe **Hold Last**, 40 Hz. Now cabled into the old eDMX4's DMX input |
| B | input | 1 | the only input (booth DMX-IN), no signal |
| C–H | output | 2–7 | nothing connected downstream |

**The chain as re-patched by Simon:** the new controller sends Art-Net
universe 0 to eDMX8 port A, which is cabled to the old eDMX4's input, which
drives the stage fixtures 1–15 (plain white). The legacy controller's USB DMX
interface is unplugged, and its app was terminated.

**Feedback loop, found and broken.** The eDMX4 (`.220`) re-broadcasts its
DMX input as Art-Net universe 0. With port A feeding that input, the eDMX4
heard its own output back: the loop held the stage at full, and HTP let no
lower level in. It was broken by unplugging the eDMX4 from the network. Its
DMX input still passes to its outputs without it. **The eDMX4 has no setting to stop re-broadcasting its input** (Simon,
29 Sep), so after the swap-over it **stays off the network**; the network is
only needed to configure it. To configure it: unplug eDMX8 port A's cable
from its input first, reconnect the network, configure, unplug the network,
then re-patch port A. This is §7.2.7's
"verify on the bench" feedback case, met in practice.

**Proved:** all 15 fixtures went off on command, then lit one at a time at
50% in order (14:44:54–14:45:36), then off. The stage is left **off**, held
by port A's Hold Last.


## The ETS project against the live installation (28 September 2026)

`site/OBHS Auditorium.knxproj` (private, gitignored; the password was removed
by Simon) holds 44 group addresses. It is **not what is programmed today**:
- The Side of Stage Touch Panel (`1.1.27`), which sent every press
  observed, has no links in it.
- `1.1.2`, which answered every stage-bank status, isn't in it at all (it was
  the legacy controller's KNX interface, confirmed 29 Sep); nor is house dimmer `1.1.19`.
- For the stage it has only `4/0/0` "Stage lights switching" and `4/0/1`
  "Stage lights dimming", while the bus uses `4/0/0`–`4/0/9` as four banks,
  their statuses, and all.

It does confirm house-light **level control**: `0/0/3` relative, `0/0/4`
absolute (set brightness), `0/0/5` value; the proscenium has the same at
`0/1/2`–`0/1/4`. Import files built from the project plus the bus capture:
`site/knx-commands-both.csv` (29 rows, direction both) and
`site/knx-feedback-incoming.csv` (23, incoming). Both parse with no warnings.


## Stage lighting rows (Simon, 28 September 2026)

Plain white fixtures on DMX universe 0 (eDMX8 port A, then the eDMX4), one
channel each:

| Row | Position | Fixtures (DMX channels) | KNX switch / status |
|---|---|---|---|
| 1 | front of stage | 1–4 | `4/0/0` / `4/0/4` |
| 2 | | 5–8 | `4/0/1` / `4/0/5` |
| 3 | | 9–12 | `4/0/2` / `4/0/6` |
| 4 | back of stage | 13–15 | `4/0/3` / `4/0/7` |
| all | | 1–15 | `4/0/8` / `4/0/9` |


## The stage swap-over (29 September 2026)

The new controller drives the stage from the wall panel. The legacy controller
application is stopped.
- **DMX:** eDMX8 port A (u0, HTP, Hold Last) feeds the eDMX4's DMX input; the
  eDMX4 is off the network (it cannot stop re-broadcasting, see above).
- **KNX:** the 5 bindings (`4/0/0-3`, `4/0/8` → "Stage row 1"–"4" and "Stage
  all") and the 5 statuses (`4/0/4-7`, `4/0/9`) are enabled. Simon tested each
  row on and off, all on and all off, and the panel indicators; all correct.
- **`1.1.2` was the legacy controller's KNX interface.** With the legacy
  controller stopped it sends nothing; the stage statuses now come only from
  this controller. There is no KNX stage actuator: the stage is DMX only.
- **Status reads get no answer**, with either knxd setting: nothing on the bus
  answers a read of `4/0/4` or `4/0/9` while the legacy interface is off.
- **knxd sends as `1.1.3`:** `KNXD_OPTS` gained `-B single` (also in
  `appliance/etc/knxd.conf.default`). The filter rewrites every outgoing
  source to the address the gateway assigned the tunnel, so no 0.0.x address
  and no client range reaches the bus, and nothing can clash with an
  undocumented 1.1.x device. `knxtool groupsocketlisten` on the controller
  still shows the local `0.0.x` source (it sees the telegram before the
  filter). Proven by the panel test after the change: the indicators the
  controller writes followed every press. The previous file is kept at
  `/data/config/knxd.conf.bak-2026-09-29`.


## The wall panel's projector button (29 September 2026)

Observed on the bus: the panel (`1.1.27`) sends `3/0/0` = `1` for on and `0`
for off, matching the ETS project's "Projector 1 Control". The feedback is
the ETS project's `3/0/1` "Projector 1 Feedback"; it can't be observed
directly (the legacy controller used to write it), but the panel's icon follows
our writes there. Projectors 2–4 (`3/1/x`–`3/3/x`) are in the ETS project
only; the auditorium has one projector.

**The panel flips its own icon when pressed**, red to green or back, before
anything happens. Hence v0.1.10's re-assert. After each press has been
handled, the controller re-sends every indicator's true value. So a press
that was refused (the projector warming or cooling, B52), failed (unreachable)
or blocked is corrected within about a second.

On the controller:
- scenes "Projector on" and "Projector off" (one `projector_power` step each);
- rules "Panel projector on" and "Panel projector off" (`3/0/0` equal 1 / 0 →
  the scene);
- status "Projector indicator": device_state, the projector,
  `on_or_warming` → `3/0/1`. Its direction was changed to both.

**Tested 29 Sep:** on and off from the panel both worked, on v0.1.9. That test
also found the indicator never went green. The status read only the connection
state, which v0.1.10 fixes. **Still to test on v0.1.10:** green through
warm-up; a second press during warm-up returns to green; a press on during
cool-down returns to red.



## The rest of the wall panel, from the bus (1 October 2026)

Simon pressed the remaining panel buttons (in no recorded order) while the
controller listened. Names come from the imported ETS project and the
observed behaviour.

| Panel sends (`1.1.27`) | ETS name | Who answers | Feedback |
|---|---|---|---|
| `0/2/0` on/off | Foyer Lights Switching | actuator `1.1.22` | `0/2/1` (from `1.1.22` and `1.1.18`) |
| `0/3/0` on/off | Stage Working Lights Switching | `1.1.18` | `0/3/1` |
| `0/5/0` on/off | Aisle Spots Switching | `1.1.22` | `0/5/1` |
| `0/6/0` on/off | Walkway Spots Switching | `1.1.22` | `0/6/1` (from `1.1.22` and `1.1.18`) |
| `1/0/3` on/off | Speakers on/off | (no feedback seen) | — |
| **`5/2/0` on/off** | **not in the ETS project** | `1.1.18` fans it out | `5/2/2` |

**`5/2/0` is a scene/mode button**, sequenced by `1.1.18`, which looks like a
logic or scene controller:
- **on:** reading light `0/7/0` on; house lights on, fading to full (`0/0/2`
  = 1, `0/0/5` = FF from dimmers `1.1.19` and `1.1.20`); Proscenium on at 80 %
  (`0/1/1` = 1, `0/1/4` = CC from `1.1.21`); walkway spots off.
- **off:** house lights and Proscenium off; walkway spots on; aisle spots off;
  working lights off; `5/2/2` = 0.

**Confirmed by Simon:** this is the panel's **"Lights on/off" button**. On
turns the house lights and Proscenium on and the walkway spots off; off does
the reverse. (The reading light, aisle spots and working lights also follow,
as seen above.)

**New devices seen:** `1.1.18` (logic/scene controller, also answers working
lights and the reading light), `1.1.21` (Proscenium dimmer), `1.1.22`
(switching actuator: foyer, aisle, walkway).

**Not pressed individually this time:** house lights `0/0/1` and Proscenium
`0/1/0`. They moved only through `5/2/0`.

**Still to capture:** the alarm's arm and disarm telegrams. Simon: arming
turns all lights off, disarming turns on only the walk spots. Expect `1.1.18`
or a `5/x` address.

**The alarm (Simon, 1 Oct):** the alarm panel sends no KNX itself. It closes
a relay that a KNX I/O module reads (high = armed, low = disarmed), and that
module sends the telegram the lights respond to (all off when armed; only the
walk spots on when disarmed).
- **The ETS project doesn't contain it.** It has no `1.1.18` (the logic/scene
  device that runs "Lights on/off" `5/2/0` and answers the working lights and
  reading light) and no dimmer `1.1.19`.
- The project's binary-input modules (`1.1.23` HDMI, `1.1.24` Projector,
  `1.1.25` Stage lights; Zennio BIN 2X/4X) look like the legacy controller's
  interfaces.
- **Most likely the alarm I/O is `1.1.18`**, sending on a `5/2/x` scene
  address.
- An overnight capture (`/data/tmp/knx-overnight.log`) will show it.

**Captured overnight, 1–2 Oct** (a listen-only unit on the CM5):
- **20:30:31:** `1.1.18` sent **`5/1/0` = 0**. Foyer, working lights,
  walkway spots, aisle spots and the reading light all reported off.
- **06:00:34:** `1.1.18` sent **`5/1/1` = 1**. Foyer and walkway spots on.
- So `5/1/0` is "all off" and `5/1/1` is the morning "walk lights on". Both
  come from `1.1.18`, the I/O and logic module. **To confirm with Simon:**
  06:00:34 looks like a timer; whether 20:30 was the alarm being set.
- The back-of-house panel (`1.1.26`) uses the same addresses as the side
  panel (`1.1.27`): projector `3/0/0`, stage `4/0/x`, "Lights on/off"
  `5/2/0`, and so on.
- `1.1.32` is the Reading Light switch (in the ETS project).
- **Proposed** (awaiting Simon): bind `5/1/0` to a critical "Alarm — All Off"
  scene taking the 15 stage fixtures to 0 %. Nothing for `5/1/1`.
