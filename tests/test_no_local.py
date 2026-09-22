"""The shipped default: rung 0 (Ollama) is disabled.

Each check runs in a fresh interpreter with LADDER_ENABLE_LOCAL removed, because
the flag is read at import time and conftest.py turns it on for the other suites.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(code: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "LADDER_ENABLE_LOCAL"}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          input=stdin, capture_output=True, text=True, timeout=60)


def check(code: str) -> None:
    res = run(code)
    assert res.returncode == 0, res.stdout + res.stderr


def test_floor_is_rung_one_and_no_kind_starts_at_zero():
    check("""
from ladder import tiers
assert not tiers.LOCAL_ENABLED and tiers.MIN_RUNG == 1
assert min(tiers.TASK_RUNGS.values()) == 1
assert tiers.resolve(kind="classify").rung == 1
""")


def test_explicit_rung_zero_is_refused_not_clamped_up():
    check("""
from ladder import tiers
for call in (lambda: tiers.by_rung(0), lambda: tiers.by_name("local"),
             lambda: tiers.resolve(rung=0)):
    try:
        call()
    except tiers.LocalTierDisabled as e:
        assert "paid" in str(e)
    else:
        raise AssertionError("rung 0 was served")
""")


def test_max_rung_zero_raises_before_any_engine_runs():
    check("""
from ladder import tiers
from ladder.router import Router
r = Router()
def boom(*a, **k):
    raise AssertionError("an engine was called")
r.engine_for = boom
try:
    r.run_job(prompt="x", kind="classify", max_rung=0)
except tiers.LocalTierDisabled as e:
    assert "max_rung=0" in str(e)
else:
    raise AssertionError("max_rung=0 ran")
""")


def test_speculation_refuses_without_a_free_drafter():
    check("""
from ladder import tiers
from ladder.speculate import Speculator, Task
try:
    Speculator(router=None).draft([Task(prompt="x")])
except tiers.LocalTierDisabled as e:
    assert "batch=true" in str(e)
else:
    raise AssertionError("speculation drafted")
""")


def test_health_does_not_probe_ollama():
    check("""
import urllib.request
from ladder.router import Router
def guard(url, *a, **k):
    u = url if isinstance(url, str) else url.full_url
    assert "11434" not in u, "probed Ollama: " + u
    raise OSError("network disabled in test")
urllib.request.urlopen = guard
h = Router().health()
assert h["ollama"]["ok"] is False and "disabled" in h["ollama"]["detail"]
""")


def test_hook_does_not_warn_about_rung_zero():
    code = ("import runpy,sys; sys.argv=['route_hint.py']; "
            "runpy.run_path('scripts/route_hint.py', run_name='__main__')")
    res = run(code, stdin=json.dumps(
        {"prompt": "add a docstring to every function in each module"}))
    assert res.returncode == 0, res.stderr
    assert "DOWN" not in res.stdout and "Ollama" not in res.stdout
    assert "ladder_spec" not in res.stdout
