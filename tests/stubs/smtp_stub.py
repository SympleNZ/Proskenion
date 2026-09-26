"""A minimal SMTP server, standalone, for the e2e Email screen test (spec
§11.4, §21.24, contracts §5).

Plain, unauthenticated, no TLS — the shape of the real relay (phase-6 plan
Q1: ``relay.n4l.co.nz``, port 25, no authentication). The same handler as
``tests/unit/core/test_email.py``'s own ``RecordingHandler``, run here as its
own process for the same reason ``cloudflare_stub.py`` is: the e2e harness
starts the real application as a subprocess, so what it talks to has to be a
process too. Every message is accepted except one addressed to
``--reject``, which gets ``550`` — §21.24's "a test button reporting inline
what failed" needs a real rejection to report, not only a success::

    uv run python -m tests.stubs.smtp_stub --port 2500 --reject bad@example.test

Prints ``listening on 127.0.0.1:<port>`` once bound.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from aiosmtpd.controller import Controller


class RecordingHandler:
    def __init__(self, *, reject_recipient: str | None) -> None:
        self.envelopes: list[Any] = []
        self._reject_recipient = reject_recipient

    async def handle_RCPT(  # noqa: N802 - aiosmtpd's own naming convention
        self, server: Any, session: Any, envelope: Any, address: str, rcpt_options: Any
    ) -> str:
        if self._reject_recipient is not None and address == self._reject_recipient:
            return "550 5.1.1 mailbox unavailable"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:  # noqa: N802
        self.envelopes.append(envelope)
        return "250 Message accepted for delivery"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--reject", default=None)
    args = parser.parse_args()

    handler = RecordingHandler(reject_recipient=args.reject)
    controller = Controller(handler, hostname="127.0.0.1", port=args.port)
    controller.start()
    print(f"listening on 127.0.0.1:{args.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
