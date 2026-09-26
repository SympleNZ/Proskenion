#!/usr/bin/env python3
"""api.py — a tiny HTTPS JSON client for the phase-6 bench scripts.

There is no ``curl`` on the appliance: ``appliance/image/build.sh``'s package
list (knxd, nginx, python3, python3-venv, python3-cryptography, smartmontools,
nvme-cli, nftables, systemd-timesyncd, logrotate, openssh-server, busybox)
does not include it, and the application's own venv is not guaranteed to be
usable — the whole point of bench B1's emergency-mode check is that the
application is not running. The standard library is on every appliance
image, so this uses only that: ``http.client``, ``ssl``, ``urllib.parse``.

Usage::

    api.py METHOD PATH [BODY]

    METHOD   GET, POST, PUT or DELETE
    PATH     a path such as /api/v1/system/backup/status, or a full
             https://... URL (used for the public /system/certs/download
             route and anything outside BENCH_BASE_URL)
    BODY     a JSON string, sent with Content-Type: application/json; or
             "-" to read the JSON body from stdin (keeps a password out of
             this process's argv, which a bench script's login() uses);
             or "@/path/to/file" to upload that file's raw bytes with
             Content-Type: application/octet-stream (a package or archive
             upload)

Environment:

    BENCH_BASE_URL    default https://localhost — the appliance talking to
                       its own nginx, which is where every bench script runs
    BENCH_COOKIE_JAR   a file this script reads and rewrites. Holds the
                       session cookie between calls; a bench script creates
                       one with mktemp and deletes it on exit
    BENCH_INSECURE    "1" (the default the bench scripts set) accepts a
                       self-signed certificate — every bench session before
                       B3 issues a trusted one needs this

Prints exactly two things to stdout: the status line ``HTTP <code>``, then
the raw response body. A caller does::

    out="$(api.py GET /api/v1/system/backup/status)"
    status="${out%%$'\\n'*}"
    body="${out#*$'\\n'}"

Exit status is 0 for any HTTP response, even 4xx/5xx — the bench script
decides what counts as a failure, the same way ``curl`` would with
``--fail`` left off. Exit status is 1 only when the connection itself could
not be made (nginx down, wrong hostname, network unreachable), which is
itself often the fact a bench check is proving.
"""

from __future__ import annotations

import http.client
import io
import os
import ssl
import sys
import urllib.parse
from typing import BinaryIO


def _read_body(arg: str | None) -> tuple[bytes | BinaryIO | None, str | None, int | None]:
    """Return (body, content_type, content_length) for the BODY argument.

    An ``@path`` body is opened and returned as a file object, not read into
    memory: an OS image or an application package can run past a gigabyte
    (``MAX_UPLOAD_BYTES`` is 2 GiB — phase-6-contracts.md), and this runs on
    the appliance itself, which has 4 GB of RAM total and an application
    already using some of it (Q1, Q9). ``http.client`` streams a body that
    has a ``read()`` method rather than buffering it whole.
    """
    if arg is None:
        return None, None, None
    if arg == "-":
        data = sys.stdin.buffer.read()
        return data, "application/json", len(data)
    if arg.startswith("@"):
        path = arg[1:]
        size = os.path.getsize(path)
        return open(path, "rb"), "application/octet-stream", size  # noqa: SIM115 - closed by the caller
    data = arg.encode("utf-8")
    return data, "application/json", len(data)


def _cookie_header(jar: str | None) -> str | None:
    if jar and os.path.exists(jar):
        with open(jar, encoding="utf-8") as fh:
            text = fh.read().strip()
        return text or None
    return None


def _save_cookie(jar: str | None, cookie_header: str | None, set_cookie: str | None) -> None:
    if not jar or not set_cookie:
        return
    existing: dict[str, str] = {}
    if cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                existing[name] = value
    # http.client folds multiple Set-Cookie headers into one comma-joined
    # string; a bench session only ever needs the one auth cookie
    # (proskenion_session), so take the first "name=value;" segment of each
    # comma-separated piece rather than parsing cookie-attribute commas.
    for piece in set_cookie.split(", "):
        name_value = piece.split(";", 1)[0].strip()
        if "=" in name_value:
            name, _, value = name_value.partition("=")
            existing[name] = value
    with open(jar, "w", encoding="utf-8") as fh:
        fh.write("; ".join(f"{k}={v}" for k, v in existing.items()))


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: api.py METHOD PATH [BODY]", file=sys.stderr)
        return 2
    method = argv[0].upper()
    path = argv[1]
    body_arg = argv[2] if len(argv) == 3 else None

    base = os.environ.get("BENCH_BASE_URL", "https://localhost")
    insecure = os.environ.get("BENCH_INSECURE") == "1"
    jar = os.environ.get("BENCH_COOKIE_JAR")

    url = path if path.startswith(("http://", "https://")) else base.rstrip("/") + path
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if host is None:
        print(f"api.py: not a URL: {url}", file=sys.stderr)
        return 1
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    body, content_type, content_length = _read_body(body_arg)
    cookie_header = _cookie_header(jar)
    headers: dict[str, str] = {}
    if cookie_header:
        headers["Cookie"] = cookie_header
    if content_type:
        headers["Content-Type"] = content_type
    if content_length is not None:
        headers["Content-Length"] = str(content_length)

    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query

    # 600s, not the usual handful of seconds: an OS image upload or a
    # verify-before-extract on the appliance's own modest CPU can take a
    # while, and this is a bench tool run once at a time, not a hot path.
    timeout_s = 600
    try:
        if parsed.scheme == "https":
            ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, context=ctx, timeout=timeout_s
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
    except OSError as exc:
        print(f"api.py: could not reach {url}: {exc}", file=sys.stderr)
        return 1
    finally:
        if isinstance(body, io.IOBase):
            body.close()

    _save_cookie(jar, cookie_header, resp.getheader("Set-Cookie"))

    sys.stdout.write(f"HTTP {resp.status}\n")
    sys.stdout.flush()
    sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
