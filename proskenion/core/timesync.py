"""Time synchronisation and degraded time mode (spec §4.9).

On boot the application waits up to :data:`BOOT_TIMEOUT_S` for NTP
synchronisation. If it arrives, startup proceeds normally. If it does not, the
application starts anyway in *degraded time mode*: an amber banner carrying
§4.9's sentence, ``[unverified time]`` in front of every log record, and a
retry every :data:`RETRY_INTERVAL_S`. On the first success the banner is
cleared, :class:`~proskenion.core.events.TimeSyncRecovered` is emitted and the
recovery is logged.

Nothing here touches JWTs. §4.9 is explicit that the elaborate monotonic-clock
handling of earlier revisions was removed: tokens are issued normally
throughout, and a wrong clock expires sessions by minutes, not weeks.

How synchronisation is detected
-------------------------------
``timedatectl show --property=NTPSynchronized --property=RTCTimeUSec``, run
through the platform layer's injectable command runner
(:data:`proskenion.core.platform.run_command`) in a worker thread. That
property is the kernel's own "clock is disciplined" flag, so it is true for
whichever NTP implementation is running and, crucially, it goes *false* again
if synchronisation is later lost.

The alternative — the presence of ``/run/systemd/timesync/synchronized`` — is
used only as a fallback when ``timedatectl`` is not installed or cannot be
run, because that file is specific to ``systemd-timesyncd`` and, once created,
is not removed when synchronisation lapses. A fallback reading is therefore
"synchronised at some point since boot", which is enough to decide whether to
raise the banner but not as good as the live flag.

RTC presence (§16.7's ``/system/time``) is read from the same ``timedatectl``
query: a machine with no RTC reports ``RTCTimeUSec=0``. The platform layer
(§5.4) exposes no RTC accessor in Phase 1, and this module deliberately does
not grow its own hardware probe to invent one — where ``timedatectl`` cannot
be consulted, RTC presence is reported as unknown (``None``) rather than
guessed.

Certificate handling
--------------------
§4.9 also requires that a degraded boot uses the self-signed fallback
certificate and skips renewal, resuming normal handling on recovery. Both are
Phase 6 (certificate management); :meth:`TimeSyncMonitor.degraded` is the flag
that work reads, and the hooks are marked below.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from proskenion.core.bus import EventBus
from proskenion.core.events import TimeSyncRecovered
from proskenion.core.platform import CommandRunner, run_command
from proskenion.core.state import StateStore, SystemWriter
from proskenion.db.crud.base import now_iso

log = logging.getLogger(__name__)

#: The banner key in ``state.system.banners``.
DEGRADED_BANNER_KEY = "time_sync"
#: §4.9's wording, verbatim. The interface shows this sentence and no other.
DEGRADED_BANNER_TEXT = (
    "System time not synchronised. Check the RTC battery and the NTP server."
)
#: How long boot waits for synchronisation before starting degraded (§4.9).
BOOT_TIMEOUT_S = 30.0
#: How often a degraded application retries (§4.9).
RETRY_INTERVAL_S = 60.0
#: How often the boot wait re-reads the flag while it is counting down.
BOOT_POLL_INTERVAL_S = 1.0
#: Prefixed to every log record while the clock is unverified (§4.9).
UNVERIFIED_PREFIX = "[unverified time]"
#: Fallback evidence when ``timedatectl`` cannot be run (see the module docstring).
SYNCHRONIZED_MARKER = Path("/run/systemd/timesync/synchronized")
#: The state-store owner name this monitor registers (§5.6, B39).
OWNER = "timesync"

_COMMAND_TIMEOUT_S = 5.0
_TIMEDATECTL = (
    "timedatectl",
    "show",
    "--property=NTPSynchronized",
    "--property=RTCTimeUSec",
)

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TimeStatus:
    """One reading of the system clock's synchronisation state (§16.7)."""

    synced: bool
    #: ``None`` when it could not be determined — never guessed.
    rtc_present: bool | None
    checked_at: str
    #: ``timedatectl``, ``marker`` or ``unavailable`` — which evidence was used.
    source: str


class UnverifiedTimeLog:
    """Prefixes every log record with ``[unverified time]`` while installed (§4.9).

    Implemented as a log-record factory rather than a handler filter because
    the prefix must survive :func:`proskenion.logging.configure_logging`
    replacing its handlers, and must reach every logger in the process — the
    requirement is that log *entries* are prefixed, not that one file's are.
    Only the format string is touched, so ``%``-style arguments still apply.
    """

    def __init__(self, prefix: str = UNVERIFIED_PREFIX) -> None:
        self._prefix = prefix
        self._previous: Callable[..., logging.LogRecord] | None = None
        self._factory: Callable[..., logging.LogRecord] | None = None

    @property
    def installed(self) -> bool:
        return self._factory is not None

    def install(self) -> None:
        if self._factory is not None:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args: object, **kwargs: object) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            record.msg = f"{self._prefix} {record.msg}"
            return record

        self._previous = previous
        self._factory = factory
        logging.setLogRecordFactory(factory)

    def remove(self) -> None:
        """Restore the previous factory, unless something else has installed one."""
        if self._factory is None:
            return
        if logging.getLogRecordFactory() is self._factory and self._previous is not None:
            logging.setLogRecordFactory(self._previous)
        else:  # pragma: no cover - another factory was installed over ours
            log.warning("log record factory was replaced; the time prefix stays in place")
        self._previous = None
        self._factory = None


class TimeSyncMonitor:
    """Boot wait, degraded time mode and the 60-second retry of §4.9.

    ``runner``, ``sleep`` and ``clock`` are injectable so the whole of §4.9 is
    testable without a system clock or a subprocess.
    """

    def __init__(
        self,
        state: StateStore,
        bus: EventBus,
        *,
        runner: CommandRunner = run_command,
        marker: Path = SYNCHRONIZED_MARKER,
        boot_timeout_s: float = BOOT_TIMEOUT_S,
        retry_interval_s: float = RETRY_INTERVAL_S,
        poll_interval_s: float = BOOT_POLL_INTERVAL_S,
        command_timeout_s: float = _COMMAND_TIMEOUT_S,
        sleep: Sleeper = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        owner: str = OWNER,
    ) -> None:
        self._state = state
        self._bus = bus
        self._runner = runner
        self._marker = marker
        self._boot_timeout = boot_timeout_s
        self._retry_interval = retry_interval_s
        self._poll_interval = poll_interval_s
        self._command_timeout = command_timeout_s
        self._sleep = sleep
        self._clock = clock
        # Several components write ``system``; every registration must agree
        # that the domain is shared (§5.6, B39).
        state.register_owner("system", owner, allow_multiple=True)
        self._writer: SystemWriter = state.system.writer(owner)
        self._prefix = UnverifiedTimeLog()
        self._degraded = False
        self._status = TimeStatus(False, None, now_iso(), "unavailable")

    # -- accessors ---------------------------------------------------------

    @property
    def degraded(self) -> bool:
        """True while the application is running on an unverified clock (§4.9)."""
        return self._degraded

    @property
    def synced(self) -> bool:
        return self._status.synced

    @property
    def status(self) -> TimeStatus:
        """The last reading, without taking a new one."""
        return self._status

    @property
    def trustworthy(self) -> bool:
        """Whether the wall clock is fit to plan a schedule rule against.

        True while synced. Also true while degraded, *if* the machine has a
        real-time clock (:attr:`TimeStatus.rtc_present` is ``True``, not
        merely unknown — §4.9's own caution against guessing) — an RTC keeps
        reasonable time across a reboot with no network, which unsynced-and-
        no-RTC does not: that boots into whatever the OS clock happened to
        be left at, and a schedule rule planned against it could fire hours
        off, or not at all (:mod:`proskenion.rules.scheduler`'s own use of
        this).
        """
        return self.synced or self._status.rtc_present is True

    # -- reading -----------------------------------------------------------

    async def read(self) -> TimeStatus:
        """Take one reading. Blocking work runs in a thread (§5.3)."""
        status = await asyncio.to_thread(self._read_sync)
        self._status = status
        return status

    def _read_sync(self) -> TimeStatus:
        properties = self._timedatectl()
        if properties is not None:
            return TimeStatus(
                synced=properties.get("NTPSynchronized") == "yes",
                rtc_present=_rtc_present(properties),
                checked_at=now_iso(),
                source="timedatectl",
            )
        try:
            present = self._marker.exists()
        except OSError:  # pragma: no cover - an unreadable /run is not our problem
            present = False
        # No ``timedatectl`` and no marker: there is no evidence source on this
        # machine at all, which is a different answer from "not yet
        # synchronised" and is what the boot wait checks before counting down.
        source = "marker" if present else "unavailable"
        return TimeStatus(present, None, now_iso(), source)

    def _timedatectl(self) -> Mapping[str, str] | None:
        try:
            result = self._runner(list(_TIMEDATECTL), self._command_timeout)
        except (OSError, ValueError) as exc:  # not installed, or not this platform
            log.debug("timedatectl unavailable: %s", exc)
            return None
        except Exception as exc:  # subprocess.TimeoutExpired and anything else
            log.debug("timedatectl failed: %s", exc)
            return None
        if result.returncode != 0:
            return None
        return _parse_properties(result.stdout)

    # -- boot (§12.1 step: wait for NTP, 30 s) ------------------------------

    async def wait_for_sync(self) -> bool:
        """Poll until synchronised or the boot timeout expires. Never raises.

        Returns ``True`` if the clock synchronised in time. On ``False`` the
        caller starts anyway — §4.9 is explicit that a flat RTC battery must
        not prevent the auditorium being controlled.

        A machine that offers no way to ask — no ``timedatectl``, no marker —
        does not get thirty seconds of waiting: the answer cannot change, and
        thirty seconds is a long time to hold up an auditorium.
        """
        deadline = self._clock() + self._boot_timeout
        while True:
            status = await self.read()
            if not status.synced and status.source == "unavailable":
                log.warning(
                    "no way to verify clock synchronisation on this machine; "
                    "starting in degraded time mode without waiting"
                )
                return False
            if status.synced:
                self._writer.set("time_synced", True)
                log.info(
                    "system clock synchronised",
                    extra={"source": status.source, "rtc_present": status.rtc_present},
                )
                return True
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            await self._sleep(min(self._poll_interval, remaining))

    def enter_degraded(self) -> None:
        """Start in degraded time mode: banner, log prefix, ``time_synced`` false.

        Certificate handling — the self-signed fallback and skipping renewal —
        is Phase 6 (§4.9); this flag is what that work reads.
        """
        if self._degraded:
            return
        self._degraded = True
        self._writer.set("time_synced", False)
        self._writer.set_banner(DEGRADED_BANNER_KEY, "amber", DEGRADED_BANNER_TEXT)
        self._prefix.install()
        log.warning(
            "starting in degraded time mode; NTP did not synchronise within %.0f s",
            self._boot_timeout,
            extra={"degraded_time": True, "timeout_s": self._boot_timeout},
        )

    # -- retry (§4.9) ------------------------------------------------------

    async def run(self) -> None:
        """Retry every 60 s while degraded; returns once the clock recovers.

        Started only when the boot wait failed. Cancellation is the ordinary
        way out at shutdown.
        """
        while self._degraded:
            await self._sleep(self._retry_interval)
            status = await self.read()
            if status.synced:
                self._recovered()

    def _recovered(self) -> None:
        """First successful synchronisation after a degraded boot (§4.9)."""
        self._degraded = False
        self._prefix.remove()
        self._writer.set("time_synced", True)
        self._writer.clear_banner(DEGRADED_BANNER_KEY)
        # Certificate handling resumes here in Phase 6 (§4.9): the renewal
        # timer is re-armed and a Let's Encrypt certificate can be validated.
        self._bus.emit(TimeSyncRecovered())
        log.info(
            "system clock synchronised; leaving degraded time mode",
            extra={"source": self._status.source},
        )

    def close(self) -> None:
        """Remove the log prefix. For shutdown and for tests."""
        self._prefix.remove()


def _parse_properties(text: str) -> dict[str, str]:
    """``KEY=value`` lines from ``timedatectl show`` into a mapping."""
    properties: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value.strip()
    return properties


def _rtc_present(properties: Mapping[str, str]) -> bool | None:
    """Whether systemd sees a hardware clock. ``None`` when it did not say."""
    raw = properties.get("RTCTimeUSec")
    if raw is None:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return None
    return int(digits) > 0

