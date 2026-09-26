# DMX Universe: Buffer, Compositor, Renderer and Fades

**Status:** production — core control logic with change-driven rendering and a unified fade engine.
**Scope:** the universe buffer, the two independent passes (DMX and KNX), fixture profiles, frame rendering, fade timing and scene precedence.
**Applies to:** §7.2.2, §7.2.3, §7.2.6, §9.5.

DMX512 addresses whole universes (up to 512 sequential values), not individual channels. The application's job is to keep correct values in a buffer per universe; Art-Net nodes regenerate the physical refresh and hold last value, so the controller need not stream frames continuously.

No hardware wire formats in the core. Lighting levels are 0–100 with one decimal; drivers convert at their own boundary (§5.5, B41). DMX wire values are 0–255; the conversion happens only in the compositor's DMX pass.

---

## 1. The universe buffer

A 512-byte buffer holds one universe. Read-only outside the compositor; created and held privately.

| | |
|---|---|
| Size | 512 slots (UNIVERSE_SIZE, `proskenion/core/dmx/universe.py:31`) |
| Max value | 255 (DMX_MAX, `proskenion/core/dmx/universe.py:34`) |
| Access | Write by index (0–based), frame retrieval as immutable bytes |
| Writer | The DMX pass, sole writer (`proskenion/core/dmx/universe.py:1–24`) |

**Fixture addressing**: DMX is 1-based (start address 1–512 as printed on fixtures); the buffer is 0-based. A fixture at DMX address 10 with a profile slot at offset 2 writes to buffer index `10 - 1 + 2 = 11`.

Source: `proskenion/core/dmx/universe.py:45–52`, `slot_index()`.

---

## 2. The compositor: two independent passes

The level store → compositor → buffers and dimmer writes. Two passes, not one, gated independently and sharing only the state store.

```
Level store  (levels 0–100 one decimal)
Group multipliers (0.0–1.0) ────┐
Master (0–100)                  ├→ Compositor
                                 ├─ DMX pass → universe buffers → frame renderer
                                 └─ KNX pass → dimmer writes    → KNX subsystem
```

**DMX pass** (`Compositor.composite_dmx()`, `proskenion/core/dmx/compositor.py`): writes to universe buffers. **Suspended when external control is active** (§7.2.7). The gate is independent; suspension does not affect KNX.

**KNX pass** (`Compositor.composite_knx()`, `proskenion/core/dmx/compositor.py`): queues dimmer writes at priority 3. Never gated by external control. House lighting runs regardless of DMX state (blocker B1).

Source: `proskenion/core/dmx/compositor.py:1–85`.

### Behaviour matrix

§7.2.3's behaviour matrix is authoritative and is not repeated here, so there is one copy to keep right. `tests/unit/core/dmx/test_behaviour_matrix.py` has one test per row, each named for its row, so a missing row is visible.

### Group multipliers

A channel in multiple groups uses the **highest multiplier** (maximum, not product). This prevents a channel from being attenuated twice if it is in overlapping groups.

A channel with `min_value > 0` is a floor the master cannot scale below: it is **exempt from both group and master scaling**, taking its clamp only. Source: `proskenion/core/dmx/compositor.py:167–176`, `resolve_level()`.

Group multipliers and the master scale DMX fixtures only. A KNX house dimmer takes its clamp and nothing else, so moving the master or a group fader sends nothing to the KNX bus (§9.5). Source: `Compositor.resolve()`, `proskenion/core/dmx/compositor.py:551–552`.

### Value scale at the boundary

The core holds levels as 0–100 with one decimal everywhere (§9.2, B5).

**DMX pass conversion**: `level_to_dmx()` converts 0–100 to 0–255 with rounding (`proskenion/core/dmx/compositor.py:148–151`).

**KNX pass conversion**: the compositor hands the KNX subsystem 0–100 (or 0–100 with one decimal), which converts to DPT 5.001 at its own boundary (§7.1). The subsystem never sees DMX values.

Source: `proskenion/core/dmx/compositor.py:154–156`, `level_to_knx()`.

---

## 3. Fixture profiles and roles

A fixture profile defines the channels (slots) it uses and their roles. Each slot has a 0-based offset from the fixture's DMX start address.

**Slot roles** (§15.9, `proskenion/core/dmx/compositor.py:109–122`):
- **Dimmer**: `"dimmer"` — brightness level; scaled by group and master
- **Colour components**: `"red"`, `"green"`, `"blue"`, `"white"` — scaled by level when the profile has no dimmer role; written unscaled when it has one, because the dimmer then carries the level alone (`proskenion/core/dmx/compositor.py:613`). White (`"w"`) is optional in RGB
- **Positional**: `"pan"`, `"tilt"`, `"strobe"`, `"macro"` — written unscaled, not affected by level, group or master. Each takes its profile default: nothing sets them yet (`proskenion/core/dmx/compositor.py:627–628`)
- **Unused**: `"unused"` — never written

Amber and UV colour components have no key in the state store; they take the profile's `default` value (0–255), scaled or unscaled as the other colour roles are.

Sources: `proskenion/core/dmx/compositor.py:109–122` (role constants), `proskenion/core/lighting.py:128–147` (profile loading).

---

## 4. The frame renderer

Change-driven output: the compositor runs when the level store changes; frames are sent only when composited data differs from the previous frame.

**Rate cap**: 40 frames per second maximum (`proskenion/core/dmx/renderer.py:72`). During a fade, frames reach this cap; at rest, the last frame is resent every second (keepalive, default `1.0 s`, bounded 0.1–2.0 s).

**Keepalive**: inside the Art-Net node's source timeout (2.5 s for E1.31); keeps the node awake when nothing is changing. A setting; default and bounds defined in `proskenion/core/dmx/renderer.py:74–79`.

**Reconnection**: when a lighting output device reconnects, it receives the current frame at once (`proskenion/core/dmx/renderer.py:24–26`).

**Frame listeners** (§7.2.3): a callback may be added to fire after a frame is sent to a device. Completion means the frame has been handed to that device without error. Called with `(device_id, frame_count)`, where `frame_count` is a counter incremented on every send, so a listener can tell when a frame has left the system.

Source: `proskenion/core/dmx/renderer.py:1–82`.

---

## 5. The fade engine

Deadline-based fade orchestration. The one funnel into the level store: every write — levels, colour and group multipliers — goes through the fade engine. A direct set is a zero-duration fade.

### Timing and easing

**Easing curve**: smoothstep, `t² × (3 − 2t)` on `t` clamped to 0–1, where `t = (now − start) / duration` (`proskenion/core/dmx/fade.py:90–93`).

**Step interval**: 50 Hz (`TICK_S = 0.02`, `proskenion/core/dmx/fade.py:71`), above the renderer's 40 fps cap. One ticker drives every active fade. Each step recomputes every fade's parameter from the clock, so a late wake-up is corrected on the next step rather than carried forward. The next wake-up is an absolute deadline: the next 20 ms tick or the earliest fade's end, whichever comes first.

**Deadline accuracy**: a fade this close to its end when a step runs is finished on that step (`END_TOLERANCE_S = 0.001 s`, `proskenion/core/dmx/fade.py:78`). This prevents a timer that fires a fraction of a millisecond early from leaving a sub-millisecond sleep.

Source: `proskenion/core/dmx/fade.py:1–93`.

### At most one fade per channel

Starting a new fade cancels the old one **from its current value** — not from the target. The old fade is evaluated at the moment of cancellation and that value stands; nothing snaps back. This lets a human interrupt a fade and the level holds at whatever the fade reached.

### Level and colour as one fade

Level and colour share one eased parameter in one fade, so a simultaneous level and colour change never shifts hue partway through.

Source: `proskenion/core/dmx/fade.py:26`.

### KNX fade modes

A KNX dimmer has a `fade_mode`: `"hardware"` or `"software"`.

- **`"hardware"`**: the dimmer drives its own fade; the fade engine sends the target value once and the dimmer ramps (§10.6, §7.2.6). The compositor sends the fade's destination, not intermediate values.
- **`"software"`**: the controller sends each step as it computes it, up to 5–10 values a second (controlled by `KNX_MIN_INTERVAL_S = 0.1 s`, `proskenion/core/dmx/compositor.py:133`). A fader dragged by hand also respects this cap.

Source: `proskenion/core/dmx/compositor.py:137`, `proskenion/core/dmx/compositor.py:127–135`.

---

## 6. Scene precedence and critical-scene locks

Every fade may carry an owner: a `SceneRun` with a priority (`"normal"` or `"critical"`, `proskenion/core/dmx/fade.py:80`, `proskenion/core/dmx/fade.py:97–111`).

**Normal scene**: operator writes beat it. A write to a channel a normal scene is fading cancels the scene's fade **on that channel only**, at its current value. The scene's fades on every other channel continue.

**Critical scene**: locks every channel it writes. A write to a locked channel from anyone but the lock holder raises `ChannelLockedError` (§10.6, `proskenion/core/dmx/fade.py:113–126`). The WebSocket handler nacks the write and turns the exception into user feedback with the scene id. Locks last until the scene engine calls `FadeEngine.release()`. Every fade a critical run starts also locks its channel (`proskenion/core/dmx/fade.py:30–50`).

Source: `proskenion/core/dmx/fade.py:1–50`.

---

## Constants

- `UNIVERSE_SIZE`: `512` (`proskenion/core/dmx/universe.py:31`)
- `DMX_MAX`: `255` (`proskenion/core/dmx/universe.py:34`)
- `LEVEL_MIN`: `0.0` (`proskenion/core/dmx/compositor.py:100`)
- `LEVEL_MAX`: `100.0` (`proskenion/core/dmx/compositor.py:101`)
- `COLOUR_MAX`: `255` (`proskenion/core/dmx/compositor.py:102`)
- `MAX_FPS`: `40` (`proskenion/core/dmx/renderer.py:72`)
- `DEFAULT_KEEPALIVE_S`: `1.0` (`proskenion/core/dmx/renderer.py:74`)
- `MIN_KEEPALIVE_S`: `0.1` (`proskenion/core/dmx/renderer.py:78`)
- `MAX_KEEPALIVE_S`: `2.0` (`proskenion/core/dmx/renderer.py:79`)
- `TICK_S`: `0.02` (50 Hz, `proskenion/core/dmx/fade.py:71`)
- `END_TOLERANCE_S`: `0.001` (`proskenion/core/dmx/fade.py:78`)
- `KNX_DEADBAND`: `0.5` % (`proskenion/core/dmx/compositor.py:130`)
- `KNX_MIN_INTERVAL_S`: `0.1` (`proskenion/core/dmx/compositor.py:133`)
