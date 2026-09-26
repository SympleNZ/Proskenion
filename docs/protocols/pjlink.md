# PJLink Class 1, as implemented

**Status:** implemented against a published Class 1 command set and the controller this
replaces; not yet run against the real PT-EZ570E — see §8 Open questions.
**Scope:** the wire protocol only, as `proskenion/core/drivers/pjlink.py` implements it.
Driver behaviour beyond the wire (capabilities, the run loop, backoff) is in that module's
own docstrings; cite §7.4 for the specification's own requirements rather than reading
either as a duplicate of the other.
**Applies to:** §7.4's PJLink client (`projector/pjlink`), `tests/stubs/pjlink_stub.py`,
`tools/pjlink_probe.py`.

---

## 1. Transport and framing

TCP port 4352 (`PJLINK_PORT`, `proskenion/core/drivers/pjlink.py:96`). Every line — the
greeting, a command, a reply — is a plain ASCII string terminated with CR (`\r`); nothing
here is length-prefixed or binary. The driver's reader tolerates a bare LF too
(`PJLinkDriver._read_line`, `proskenion/core/drivers/pjlink.py:318-326`) rather than
insisting on the exact byte the published spec names, since getting a CR-only device wrong
costs nothing and getting a CRLF-only device wrong would hang a read.

**One TCP connection per command.** Open, read the greeting, prefix the authentication
digest if asked for one, send exactly one command, read exactly one reply, close
(`PJLinkDriver._run_command`, `proskenion/core/drivers/pjlink.py`). This matches
`docs/hardware/legacy-controller.md`'s account of the controller this replaces, and is
followed for every operation — discovery at connect, the health probe, `set_power`,
`set_input` and `read_state` each open and close their own connection. No PJLink client in
this codebase holds a connection open between commands.

**One exchange at a time, serialised by a lock.** The health probe runs from the base
driver's own periodic task (`Driver.maintain`, §5.3) while `set_power`, `set_input` and
`read_state` are called by the service layer whenever an operator acts — the two are not
coordinated with each other, so they can land at the same moment. `PJLinkDriver` holds one
`asyncio.Lock` for the whole of `_run_command` — open, greeting, authenticate, send, read,
close — so only one such exchange is ever in flight. Without it, two overlapping calls would
each open their own connection, and **many PJLink projectors accept only one connection at a
time**, refusing or resetting a second; the loser would look like a transient failure, or
worse, an authentication failure, incrementing the counter §6 describes for no real
authentication reason. `tests/stubs/pjlink_stub.py` enforces the same one-at-a-time rule
(`PJLinkStub._busy`) so `test_concurrent_probe_and_set_power_are_serialised`
(`tests/unit/core/drivers/test_pjlink.py`) fails without the driver's lock and passes with
it. **The "only one connection at a time" premise itself is not yet confirmed against the
real PT-EZ570E** — see §8.

## 2. Greeting and authentication (§7.4)

The projector speaks first: `PJLINK 0\r` (no authentication) or `PJLINK 1 <nonce>\r`
(authenticate this connection). `<nonce>` is a random alphanumeric string, generated fresh
per connection — the stub uses 8 hex characters (`PJLinkStub._new_nonce`,
`tests/stubs/pjlink_stub.py`), comfortably inside the published spec's 32-character limit,
and nothing in the protocol requires a longer one.

When authentication is asked for, the client prefixes the command with the hex MD5 of
`nonce + password` (`PJLinkDriver._digest`, `proskenion/core/drivers/pjlink.py:173-177`):

```
<32 hex chars><command>\r
```

Three cases (§7.4's exact wording, `proskenion/core/drivers/pjlink.py:102-103`):

| Case | Result | Message |
|---|---|---|
| required, no password configured | red | `MSG_AUTH_NONE_CONFIGURED` — "Authentication required — set the password in device settings" |
| required, wrong password | red | `MSG_AUTH_WRONG_PASSWORD` — "Authentication failed — check the password." |
| not required, a password is configured anyway | ignored | connects without a digest |

A wrong digest answers `PJLINK ERRA\r` in place of the command's own reply — not
`%1<CMD>=ERRA`, and not scoped to the command that was sent. The driver treats any `PJLINK
ERRA` reply, to any command, as an authentication failure (`PJLinkDriver._run_command`,
`proskenion/core/drivers/pjlink.py:376-377`).

**The "no password configured" case never reaches the wire.** If the greeting asks for
authentication and none is configured, the driver does not attempt the command at all —
there is no password to try, so failing locally after reading the greeting is one round
trip cheaper than sending a doomed request.

## 3. Command set used

All Class 1, matching `docs/hardware/legacy-controller.md`'s account of the controller this
replaces:

| Command | Direction | Used for |
|---|---|---|
| `%1POWR ?` | query | discovery at connect, the health probe, `read_state` |
| `%1POWR 0` / `%1POWR 1` | control | `set_power` |
| `%1INPT ?` | query | `read_input`, called by the projector service (P3-T4) after a state change to `on` and after `set_input` — never on a poll of its own |
| `%1INPT <ref>` | control | `set_input`; `<ref>` is one of `%1INST ?`'s own codes |
| `%1INST ?` | query | capabilities, at connect |
| `%1CLSS ?`, `%1INF1 ?`, `%1INF2 ?`, `%1INFO ?`, `%1NAME ?`, `%1ERST ?`, `%1LAMP ?` | query | `tools/pjlink_probe.py` only — the driver never sends these |

`%1POWR ?` doubles as both the discovery query and the §11.1 health probe, so the same
one-line exchange that answers "is the projector alive" also answers "what state is it in"
(`PJLinkDriver.probe`, `proskenion/core/drivers/pjlink.py:431-449`).

## 4. Replies and error codes (§7.4)

A successful query reply is `%1<CMD>=<value>`; a successful control command replies
`%1<CMD>=OK`. `PJLinkDriver._parse_reply` (`proskenion/core/drivers/pjlink.py:180-190`)
strips the `%1<CMD>=` prefix and hands the driver the value.

`%1POWR ?`'s four success values map straight onto `ProjectorState` (§7.4, verbatim):

| Value | State |
|---|---|
| `0` | `OFF` |
| `1` | `ON` |
| `2` | `COOLING` |
| `3` | `WARMING` |

Five error codes, all defined in `docs/hardware/legacy-controller.md` and the published
Class 1 specification:

| Code | Meaning | This driver's handling |
|---|---|---|
| `ERR1` | undefined command | on a query (`%1POWR ?`): `ProjectorState.ERROR`. On a control command: `PJLinkCommandError` |
| `ERR2` | out of parameter | same as `ERR1` |
| `ERR3` | unavailable time (warming/cooling refused it) | only ever expected on a control command: `ProjectorBusyError`, carrying the state that caused the refusal (§7.4, B52) |
| `ERR4` | projector/display failure | on a query: `ProjectorState.ERROR`, and the probe still reports the device *alive* — it answered, so the connection works even though the display has faulted. On a control command: `PJLinkCommandError` |
| `ERRA` | authentication failure | `PJLinkAuthenticationError`, §2 above |

`ERR3` in reply to `%1POWR ?` itself is not part of the published protocol (queries are not
documented as ever refusing with "unavailable time") and is treated the same as `ERR1`/`ERR2`
— logged and mapped to `ProjectorState.ERROR` — rather than raised, so a device that
surprises the driver here still yields a state rather than crashing the poll
(`PJLinkDriver._apply_power_reply`, `proskenion/core/drivers/pjlink.py:516-526`).

## 5. Capabilities (§5.5)

`%1INST ?` answers a space-separated list of PJLink's own two-character input codes, e.g.
`11 21 31 32` (`PJLinkDriver._parse_inst`, `proskenion/core/drivers/pjlink.py:193-196`).
Each code is opaque to the core — never decoded, labelled only by whatever the admin screen
does with it — and becomes `ProjectorCapabilities.inputs`, resolved once at `connect()` and
unchanged until the next reconnect.

## 6. Authentication failure backoff (§7.4)

After three consecutive authentication failures (either mismatch case in §2, or an
unexpected `ERRA` on any later command), the run loop's backoff holds at a fixed 60 seconds
instead of continuing to double (`PJLinkDriver._backoff`,
`proskenion/core/drivers/pjlink.py:500-515`). A wrong or missing password needs a human in
device settings; nothing about trying again sooner fixes it, and the venue's PJLink stack
does not need the extra connection attempts.

**Colour** (reconciled by P3-T4). §7.4 asks for this hold to show amber. The device-status
colour mapping (`proskenion/core/devices.py`'s `DeviceManager._operator_status`) derives
amber ("degraded") only from a *device*-kind failure following a previously *successful*
connection — the usual case here, a password that has never worked, has no prior good
connection to be degraded from, and would show red under that rule alone. `PJLinkDriver`
now exposes `auth_holding` (`proskenion/core/drivers/pjlink.py`), true for the whole of the
60 s hold; `DeviceManager` reads it — duck-typed, exactly as `latency_ms` already is — and
shows `degraded` whenever it is true, whatever `connected_once` says. No other driver
exposes this attribute, so no other device's colour changes.

## 7. Health and boot (§7.4, §11.1)

`connect()` queries `%1POWR ?` and `%1INST ?` and sends nothing else — never a power
command, so a projector mid-cycle from before a restart is discovered, not overwritten.

Probe cadence, chosen per discovered state (`PJLinkDriver.maintain`,
`proskenion/core/drivers/pjlink.py:451-489`):

| State | Interval | Source |
|---|---|---|
| `on` | 30 s | §7.4, §11.1, verbatim |
| `off` | 5 min | §7.4, §11.1, verbatim |
| `warming` / `cooling` | 5 s | not specified — see below |
| `error` | 30 s | not specified — treated the same as `on` |
| `unreachable` | the run loop's own exponential backoff, capped at 300 s (§5.3) | not specified; this state is never current while `maintain()` is running |

**Why 5 s for a transition.** Neither figure is in the specification. A lamp projector
typically warms or cools in under two minutes; §7.4 wants a rule to fire "the moment the
projector settles" once the transition ends (via the `device_state` trigger P3-T4 adds), so
the probe interval needs to be short relative to the transition for "the moment" to be a
fair description. 5 s keeps the detected transition within a few seconds of the real one
without meaningfully loading a projector that is already busy warming up.

**Why 30 s for `error`.** Not specified. Treated the same as `on` rather than the idle `off`
cadence, on the basis that a faulted projector is presumably about to be attended to and
recovery should be noticed as promptly as normal operation.

## 8. Open questions (for the bench)

Run against the real PT-EZ570E at `10.2.30.249` on 21 September 2026 with
`tools/pjlink_probe.py` (`docs/plans/phase-3.md` Q2). Four of the five below are answered
and keep their answers here; the fifth needs two connections at once and the probe opens
one by design. The readings are also in `docs/hardware/network_map.md`.

- **Does the PT-EZ570E answer `%1CLSS ?` with `2`?** If so, §21.14's cooling countdown
  becomes possible in a later phase; Class 1 alone cannot show it (the phase-3 plan already
  records this). **Answered: `1`.** No countdown, and nothing assumed one. Class 1 still
  answers `%1POWR ?` with `0`/`1`/`2`/`3` — off, on, cooling, warming — so the state and
  the disabled-with-a-reason buttons of §21.14 and §7.4 are unaffected; only the *seconds
  remaining* is unavailable, which §21.14 already hedged as "where PJLink reports it".
- **What does `%1INST ?` actually list for this projector?** The driver treats every code
  as opaque, so any answer is handled, but the admin picker will only be as useful as what
  is actually on the unit. **Answered:** `11 12 13 21 22 23 24 25 31 32 33 46 47 48`.
  Fourteen codes, all handled as opaque. Firmware 1.01 / network 2.00, projector name
  `AUDITORIUM`, lamp 1172 hours, error status `000000`.
- **How does the PT-EZ570E behave when a connection is opened and then left idle** — does
  it time out and close its end, and if so after how long? The one-connection-per-command
  pattern never relies on an idle connection surviving, so this mostly matters for how
  quickly a stuck connection from some other client (MixPad-equivalent, a stray script)
  clears itself. **Answered: it closes its own end after 30 s of silence**, so a stuck
  connection clears itself within half a minute without anyone intervening. A fresh connect
  with MD5 authentication costs **45–90 ms** over ten attempts, which is the other half of
  the answer: holding a connection open between polls would buy back tens of milliseconds
  and cost a timeout to trip over. The one-connection-per-command pattern stands.
- **Whether the venue's authentication is genuinely on**, confirmed directly rather than
  taken on `docs/hardware/legacy-controller.md`'s word, and whether the password recorded
  there still works. **Answered: authentication is on, and the recorded password works.**
  The password stays in `HANDOVER.md` and appears neither here nor anywhere else in this
  repository — including by description, since a projector password worth naming is a
  projector password worth guessing. Whether it should be changed at commissioning is a
  question for Simon, and if it is changed, `HANDOVER.md` changes in the same breath.
- **Does the PT-EZ570E genuinely refuse or reset a second simultaneous PJLink connection?**
  This is the premise behind the driver's `_lock` (§1 above) and is not confirmed — the
  legacy controller's one-connection-per-command pattern is consistent with it needing to be
  true, but consistent is not proof. `tools/pjlink_probe.py` opens one connection at a time
  by design and so cannot itself test this; confirming it means opening two connections to
  the real unit concurrently (by hand, or a small addition to the probe) and observing
  whether the second is refused, hangs, or is served normally alongside the first.
  **Answered, 21 September 2026: it accepts exactly one, and a second one hangs.**
  Three connections were opened in turn. The first was accepted in 24 ms and greeted
  normally; the second and third completed no handshake at all and timed out after
  5 s, while the first stayed open and healthy throughout. So the premise behind
  `_lock` holds — the unit really does serve one client — but **not in the shape
  §1's comment assumes**. That comment expects the loser to "fail outright or, worse,
  look exactly like a wrong password"; what actually happens is that it stalls, and
  `TCPTransport.CONNECT_TIMEOUT` (10 s) is what bounds it. This matters beyond our own
  locking, because the venue's legacy controller is still in the rack: if anything else
  holds the projector, every command of ours costs 10 s and then fails as a connect
  timeout, and the 30 s idle timeout above is what eventually frees it.
  **Resolved (P7-T4):** the connect timeout is classified *busy* (amber), not *offline*
  (red). `PJLinkDriver.connect()` and `.probe()` now catch the transport's
  `ConfigurationError` and inspect `exc.__cause__`: a `TimeoutError` cause — exactly the
  shape this bench found — sets `connect_busy` and is reported through the same
  device-kind path as §7.4's authentication hold, rather than propagating as a
  `config`-kind failure. A refused connection, an unresolved name or any other
  `ConfigurationError` still propagates unchanged and stays red. `DeviceManager`
  (`proskenion/core/devices.py`) reads `connect_busy` exactly as it reads `auth_holding`
  and the CQ-20B's `amber_failure`.

---

## Constants

- `PJLINK_PORT`: `4352` (`proskenion/core/drivers/pjlink.py:96`)
- `AUTH_FAILURE_THRESHOLD`: `3` (`proskenion/core/drivers/pjlink.py:107`)
- `AUTH_HOLD_SECONDS`: `60.0` (`proskenion/core/drivers/pjlink.py:108`)
- `PJLinkDriver.COMMAND_TIMEOUT`: `2.0` s per command (`proskenion/core/drivers/pjlink.py:223`)
- `PJLinkDriver.ON_INTERVAL` / `OFF_INTERVAL` / `TRANSITION_INTERVAL` / `ERROR_INTERVAL`:
  `30.0` / `300.0` / `5.0` / `30.0` s (`proskenion/core/drivers/pjlink.py:226-229`)
