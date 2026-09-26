# Proskenion

Auditorium AV control appliance for a school theatre: lighting over KNX and DMX, an
Allen & Heath CQ-20B mixer, a PJLink projector and an HDMI matrix, from one tablet
or booth screen. Runs on a Raspberry Pi CM5 with a read-only root filesystem, A/B
OS slots and a hardware watchdog. Replaces an ageing Windows/.NET system.

*Proskenion* (προσκήνιον) is the raised stage of a Hellenistic Greek theatre, the
word English took "proscenium" from. The name carries no technical meaning.

## Documents

| File | Purpose |
|---|---|
| `docs/proskenion-spec-v3.1.html` | The specification — source of truth |
| `docs/CLAUDE-CODE-BRIEF.md` | How the build is coordinated |
| `CONVENTIONS.md` | Code style and the non-negotiable rules, each with its spec section |
| `ARCHITECTURE.md` | Why, not what |
| `WORKLOG.md` | Verified work and recorded deviations |
| `CHANGELOG.md` | Per-release changes |
| `docs/protocols/` | Protocol references, including the reverse-engineered CQ native protocol |

## Stack

Python 3.13 · FastAPI · asyncio · aiosqlite (WAL) · pyserial-asyncio
React 19 · TypeScript 7 · Vite 8 · Vitest · Tailwind CSS 4
Raspberry Pi OS Lite 64-bit (Debian 13) · knxd · nginx · systemd

## Quick start (development)

```
uv sync
uv run pytest
```

The frontend lives in `web/` and is scaffolded in Phase 1. The JavaScript
toolchain never runs on the appliance; a built bundle ships in the package.

## Layout

```
proskenion/    Python package — core/, rules/, scene/, db/, api/, main.py (spec §5.2)
web/           React source and build output
tests/         unit/ integration/ e2e/ hil/ stubs/ (spec §22)
appliance/     systemd units, nginx, udev, golden image build (spec §4)
docs/          specification, brief, mockups, protocols/, hardware/, build/
tools/         bench tools that are not part of the appliance (cq_probe.py)
```

Built by Simon Wright, Symple Solutions, Dunedin, New Zealand.
