"""The 72-hour soak harness (spec §22.7, decision D3).

``python -m tests.soak --help``. The runbook is ``docs/hardware/soak-test.md``.

- :mod:`tests.soak.plan` — the timetable, and a time-compression factor for rehearsals
- :mod:`tests.soak.rig` — the device stubs on fixed loopback ports (stage 1)
- :mod:`tests.soak.api` — the admin session, REST and the WebSocket
- :mod:`tests.soak.provision` — commissions the throwaway soak database through the API
- :mod:`tests.soak.procfs` — the outside measurements: ``/proc``, ``/sys``, the database files
- :mod:`tests.soak.run` — runs the load, samples, and records everything to a results directory
- :mod:`tests.soak.scoring` — the §22.7 pass/fail criteria and the report

Nothing here ships in the application package. On the CM5 it runs from a
copy of ``tests/__init__.py``, ``tests/stubs`` and ``tests/soak`` with the
application's own interpreter, which already has everything it imports.
"""
