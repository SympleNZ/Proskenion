"""A minimal Cloudflare-shaped HTTP stub, standalone (spec §3.2, contracts §5).

Implements the three calls :class:`proskenion.core.cloudflare.CloudflareClient`
makes — token verify (a zone list), create a TXT record, delete it — and
forwards the record itself to letsencrypt/pebble-challtestsrv's management
API, so a real Pebble ACME server validates DNS-01 against it. The same
handler as ``tests/integration/certs/test_acme_pebble.py``'s in-process
``_StubHandler``, run here as its own process: that test hands a pytest
fixture's stub straight to an in-process ``CertificateManager``, which a real
application started as a subprocess (``tests/e2e``'s own harness) cannot —
this is what lets an end-to-end Playwright test reach the same seam::

    uv run python -m tests.stubs.cloudflare_stub --port 8899 \\
        --hostname pebble-cert-test.example --challtestsrv http://localhost:8055

Prints ``listening on 127.0.0.1:<port>`` once bound, and nothing else on a
quiet run — the stub keeps no HTTP access log.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx


def build_handler(hostname: str, challtestsrv_url: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
            pass

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/zones"):
                self._reply(200, {"success": True, "result": [{"id": "zone-1", "name": hostname}]})
                return
            self._reply(404, {"success": False, "errors": [{"message": "not found"}]})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/zones/zone-1/dns_records":
                body = self._body()
                name = str(body.get("name"))
                value = str(body.get("content"))
                httpx.post(
                    f"{challtestsrv_url}/set-txt",
                    json={"host": f"{name}.", "value": value},
                    timeout=10,
                ).raise_for_status()
                self._reply(200, {"success": True, "result": {"id": name}})
                return
            self._reply(404, {"success": False, "errors": [{"message": "not found"}]})

        def do_DELETE(self) -> None:  # noqa: N802
            prefix = "/zones/zone-1/dns_records/"
            if self.path.startswith(prefix):
                name = self.path[len(prefix) :]
                httpx.post(
                    f"{challtestsrv_url}/clear-txt", json={"host": f"{name}."}, timeout=10
                ).raise_for_status()
                self._reply(200, {"success": True, "result": {"id": name}})
                return
            self._reply(404, {"success": False, "errors": [{"message": "not found"}]})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--challtestsrv", default="http://localhost:8055")
    args = parser.parse_args()

    handler = build_handler(args.hostname, args.challtestsrv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    server.daemon_threads = True
    print(f"listening on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
