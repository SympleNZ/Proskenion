"""Drivers (spec §5.5).

Every driver ships with the application and is registered in code — there is
no plugin loading, no entry-point scanning and no auto-discovery (§5.5 *What
this excludes*, §6.11). :func:`load_shipped_drivers` is the one place a driver
module is imported; importing it runs its ``@register`` decorator.

The list is a function rather than module-level imports because the transport
package imports :mod:`proskenion.core.drivers.fields`, which initialises this
package; eager imports here would be a circular import whenever a transport is
imported first. The registry calls this before answering, so nothing depends
on the application remembering to.
"""

from __future__ import annotations

_loaded = False


def load_shipped_drivers() -> None:
    """Import every shipped driver module, once. Explicit — no scanning."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    # Shipped drivers are imported here, one line each.
    from proskenion.core.dmx import drivers as _dmx_drivers  # noqa: F401
    from proskenion.core.drivers import cq20b as _cq20b  # noqa: F401
    from proskenion.core.drivers import lkv422 as _lkv422  # noqa: F401
    from proskenion.core.drivers import pjlink as _pjlink  # noqa: F401
    from proskenion.core.drivers import (
        stub_matrix,  # noqa: F401
        stub_mixer,  # noqa: F401
    )
