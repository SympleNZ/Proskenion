"""The application, started as the appliance starts it, with the serial bridge installed.

    uv run python -m tests.stubs.bridged_app --config <path>

For the Phase 3 browser journeys (``tests/e2e/av-milestone.spec.ts``), which
need the application's own ``lkv422`` device to reach the matrix stub in the
stub process (:mod:`tests.stubs.control`). Before handing over to
:func:`proskenion.main.main` — the ``proskenion`` entry point itself, with the
same arguments — it installs, from the environment:

- :data:`~tests.stubs.serial_bridge.BRIDGE_ENV`: device paths to open as TCP
  connections to the stub (:func:`~tests.stubs.serial_bridge.install_from_environment`)
- :data:`MATRIX_PROBE_ENV`: the LKV422's probe interval, in seconds, in place
  of the production 25 s, so a front-panel journey does not wait out a real
  poll — ``tests/integration/test_phase3_milestone.py`` proves the production
  interval separately
- :data:`CQ_NATIVE_PORT_ENV`: the CQ-20B's native (metering) port, in place of
  the desk's fixed 51326, so the driver's second connection reaches the native
  stub on the loopback port it was given (``tests/e2e/mixer-milestone.spec.ts``)

Nothing in ``proskenion/`` changes; this is the harness choosing how the
process under test is started, as ``config.toml`` already chooses its ports.
"""

from __future__ import annotations

import os
import sys

from proskenion.core.drivers.cq20b import CQ20BDriver
from proskenion.core.drivers.lkv422 import LKV422Driver
from proskenion.main import main
from tests.stubs.serial_bridge import install_from_environment

#: Seconds between the LKV422's probes — its routing poll (§7.5, §11.1).
MATRIX_PROBE_ENV = "PROSKENION_TEST_MATRIX_PROBE_S"
#: The CQ-20B's native port (§7.3), on the same host as its MIDI transport.
CQ_NATIVE_PORT_ENV = "PROSKENION_TEST_CQ_NATIVE_PORT"


def run() -> int:
    bridges = install_from_environment()
    probe = os.environ.get(MATRIX_PROBE_ENV)
    if probe:
        LKV422Driver.PROBE_INTERVAL = float(probe)
    native = os.environ.get(CQ_NATIVE_PORT_ENV)
    if native:
        CQ20BDriver.NATIVE_PORT = int(native)
    print(
        f"bridged_app: serial bridges {bridges or 'none'}; matrix probe "
        f"{LKV422Driver.PROBE_INTERVAL:g} s; CQ native port {CQ20BDriver.NATIVE_PORT}",
        file=sys.stderr,
        flush=True,
    )
    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(run())
