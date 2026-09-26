#!/usr/bin/env bash
# check-bench-scripts.sh — static acceptance checks for tests/hil/phase6/.
#
# A sibling to check-units.sh, not an addition to it: that file checks
# appliance/ against the specification's directives; this one checks the
# bench scripts against the requirement that a person alone at a bench
# can run them safely. No root, no Docker, runs under Git Bash, like
# check-units.sh. Called as its own stage from verify-in-docker.sh, right
# after check-units.sh.
#
# What "self-consistent" means here, made concrete:
#   - every PASS/FAIL label is unique within its script — a label used more
#     than once as pass, or more than once as fail, means two different
#     checks were given the same name, which is exactly what would make a
#     transcript sent back ambiguous about which one failed;
#   - every destructive step is behind a confirmation — found by the
#     "# DESTRUCTIVE:" comment convention: bin/bench.sh's fail()/confirm()
#     header explains why the convention exists, and every occurrence here
#     is checked to be immediately followed by a call to confirm();
#   - the scripts parse (bash -n) and the Python helpers compile
#     (py_compile) — shellcheck itself runs in verify-in-docker.sh's stage 1,
#     in the same list as every other appliance script, not duplicated here.
#
# It does not run any bench script — nothing here touches real hardware,
# and nothing here starts a server or opens a socket. It reads the scripts
# as text.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
HIL_DIR="../tests/hil/phase6"

fail=0
pass() { printf 'ok    %s\n' "$1"; }
flunk() { printf 'FAIL  %s\n' "$1"; fail=1; }

[ -d "$HIL_DIR" ] || { echo "FAIL  ${HIL_DIR} does not exist"; exit 1; }

SCRIPTS=("${HIL_DIR}"/b*.sh)
LIB_SH=("${HIL_DIR}"/lib/*.sh)
LIB_PY=("${HIL_DIR}"/lib/*.py)

# --- every script parses ----------------------------------------------------
for f in "${SCRIPTS[@]}" "${LIB_SH[@]}"; do
    if bash -n "$f" 2>/tmp/bench-syntax.err; then
        pass "bash -n: $(basename "$f")"
    else
        flunk "bash -n: $(basename "$f") — $(cat /tmp/bench-syntax.err)"
    fi
done

# --- the Python helpers compile ---------------------------------------------
for f in "${LIB_PY[@]}"; do
    if python3 -m py_compile "$f" 2>/tmp/bench-pyc.err; then
        pass "py_compile: $(basename "$f")"
    else
        flunk "py_compile: $(basename "$f") — $(cat /tmp/bench-pyc.err)"
    fi
done

# --- every script has a shebang, no CRLF ------------------------------------
for f in "${SCRIPTS[@]}" "${LIB_SH[@]}" "${LIB_PY[@]}"; do
    if head -1 "$f" | grep -q '^#!'; then
        pass "shebang: $(basename "$f")"
    else
        flunk "shebang: $(basename "$f") is missing one"
    fi
done
# Not grep -l $'\r': under Git Bash on Windows this tool runs on, that has
# been observed to report no match even against a file deliberately written
# with CRLF — some layer between here and grep's own byte reading normalises
# it away before the pattern ever sees it. python3's binary read does not
# have that problem, and it is on every platform this needs to run on.
# Only the scripts themselves, not __pycache__'s .pyc files, which are
# binary and can contain the byte pair 0x0D 0x0A by pure coincidence.
crlf_files="$(python3 -c "
import pathlib, sys
paths = [pathlib.Path(p) for p in sys.argv[1:]]
hits = [str(p) for p in paths if b'\r\n' in p.read_bytes()]
print(' '.join(hits))
" "${SCRIPTS[@]}" "${LIB_SH[@]}" "${LIB_PY[@]}")"
if [ -n "$crlf_files" ]; then
    flunk "CRLF line endings found under ${HIL_DIR}: ${crlf_files}"
else
    pass "no CRLF line endings under ${HIL_DIR}"
fi

# --- PASS/FAIL label uniqueness, and confirm-gated destructive steps -------
# One small Python script does both — it is the same rule for every file, and
# writing it as an awk one-liner twice (once per rule) would be harder to
# read than the rule it enforces. It changes nothing on disk.
#
# Two Windows/Git-Bash-specific things shaped how this is invoked, both
# found by actually running this against the real thing rather than assumed:
#   - `python3 - <<HEREDOC` (piping the script straight to python3's stdin)
#     has been observed to crash Git Bash itself here ("cygheap read copy
#     failed... couldn't create signal pipe") — a `cat` heredoc into a file
#     does not, so the write and the run are two separate commands.
#   - a `mktemp` path is POSIX-absolute (/tmp/tmp.XXXXXX). verify-in-docker.sh
#     sets MSYS_NO_PATHCONV=1 for its own Docker mounts, and that is still
#     set in this shell when it calls this script as its stage 6 — with it
#     set, Git Bash does not translate that path for a native python.exe,
#     which then looks for it under a literal C:\tmp and fails. A relative
#     path in the current directory (appliance/, per this file's own `cd`
#     above) needs no translation either way.
BENCH_CHECK_PY="./.bench-check-scripts-$$.py"
trap 'rm -f "${BENCH_CHECK_PY}"' EXIT
cat >"${BENCH_CHECK_PY}" <<'PYEOF'
import re
import sys

PASS_RE = re.compile(r'\bpass "([^"]*)"')
FAIL_RE = re.compile(r'\bfail "([^"]*)"')
DESTRUCTIVE_RE = re.compile(r'^\s*#\s*DESTRUCTIVE:\s*$')
CONFIRM_RE = re.compile(r'\bconfirm\s+"')
COMMENT_ONLY_RE = re.compile(r'^\s*#')
BLANK_RE = re.compile(r'^\s*$')

overall_ok = True

for path in sys.argv[1:]:
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines()
    name = path.rsplit("/", 1)[-1]

    # -- label uniqueness: a label used more than once as pass, or more than
    #    once as fail, names two different checks the same thing.
    from collections import Counter
    pass_counts = Counter(PASS_RE.findall(text))
    fail_counts = Counter(FAIL_RE.findall(text))
    dupes = sorted({l for l, c in pass_counts.items() if c > 1} |
                    {l for l, c in fail_counts.items() if c > 1})
    if dupes:
        print(f"FAIL  {name}: PASS/FAIL label(s) used more than once: {dupes}")
        overall_ok = False
    else:
        print(f"ok    {name}: every PASS/FAIL label is unique")

    # -- every "# DESTRUCTIVE:" is immediately followed (skipping blank and
    #    comment lines) by a call to confirm().
    bad_destructive = []
    for i, line in enumerate(lines):
        if not DESTRUCTIVE_RE.match(line):
            continue
        j = i + 1
        while j < len(lines) and (BLANK_RE.match(lines[j]) or COMMENT_ONLY_RE.match(lines[j])):
            j += 1
        if j >= len(lines) or not CONFIRM_RE.search(lines[j]):
            bad_destructive.append(i + 1)
    if bad_destructive:
        print(f"FAIL  {name}: '# DESTRUCTIVE:' not immediately followed by confirm(...) at line(s) {bad_destructive}")
        overall_ok = False
    elif "# DESTRUCTIVE:" in text or DESTRUCTIVE_RE.search(text):
        print(f"ok    {name}: every '# DESTRUCTIVE:' is immediately followed by confirm(...)")
    else:
        print(f"ok    {name}: no destructive steps (nothing to check)")

sys.exit(0 if overall_ok else 1)
PYEOF
if python3 "${BENCH_CHECK_PY}" "${SCRIPTS[@]}"; then
    pass "labels and confirm-gating: all scripts compliant"
else
    flunk "labels and confirm-gating: see the FAIL lines above"
fi

echo
if [ "$fail" = 0 ]; then echo "check-bench-scripts: all checks passed"; else echo "check-bench-scripts: FAILURES above"; fi
exit "$fail"
