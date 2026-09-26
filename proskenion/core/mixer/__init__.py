"""Mixer subsystem (spec §7.3, §5.5, §15.6).

Holds the CQ-20B's two independent connections and the mixer service that
owns ``state.mixer``: :mod:`proskenion.core.mixer.native`, the read-mostly
metering client, and :mod:`proskenion.core.mixer.service`, the service
layer — the MIDI control client lives with the CQ-20B driver
(:mod:`proskenion.core.drivers.cq20b`), as separate work from either.
"""

from __future__ import annotations
