"""Local model lifecycle: what is installed, what is resident, what fits.

Ladder is usually the only thing on a machine running local models, so it may as
well manage them rather than leaving it to the user to remember `ollama pull`,
`ollama ps` and `ollama stop`.

Three facts drive everything here, all measured on the development machine:

* **Residency dominates latency.** A cold 18 GB model costs ~33 s to page in
  against 0.3 s warm. Warming the model before a batch is worth more than any
  amount of tuning after it.
* **Only one model should usually be resident.** Each one holds its full weight
  in RAM whether or not it is being used; two idle models cost gigabytes for
  nothing.
* **A model that does not fit in RAM is not slow, it is unusable.** Paging
  weights from NVMe during generation is orders of magnitude slower than RAM,
  and for a mixture-of-experts it is worse still, because expert selection
  changes every token and turns it into random reads.

Standard library only, matching the Ollama engine.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .engines.ollama_engine import DEFAULT_HOST, KEEP_ALIVE

# Leave this much RAM for the rest of the machine. Filling memory to the brim
# just moves the pain to whatever the user is actually doing.
HEADROOM_GB = 8.0


def _get(host: str, path: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(f"{host}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(host: str, path: str, payload: dict, timeout: int = 900) -> dict:
    req = urllib.request.Request(
        f"{host}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def available_ram_gb() -> float | None:
    """Physical memory available for a new model, or None if unknown.

    Uses the OS notion of *available* rather than *free*: Windows counts
    reclaimable cache as in-use, which would understate headroom badly.
    """
    try:
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = Status()
        st.dwLength = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullAvailPhys / 1e9
    except Exception:  # noqa: BLE001 - non-Windows, or ctypes unavailable
        pass
    try:  # Linux / macOS via os.sysconf where present
        import os

        return (os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / 1e9
    except Exception:  # noqa: BLE001
        return None


def installed(host: str = DEFAULT_HOST) -> list[dict]:
    """Models pulled to disk, with their sizes."""
    try:
        data = _get(host, "/api/tags", timeout=10)
    except Exception:  # noqa: BLE001
        return []
    return [{"name": m["name"], "size_gb": m.get("size", 0) / 1e9}
            for m in data.get("models", [])]


def resident(host: str = DEFAULT_HOST) -> list[dict]:
    """Models currently held in RAM, with how long they will be kept."""
    try:
        data = _get(host, "/api/ps", timeout=10)
    except Exception:  # noqa: BLE001
        return []
    return [{"name": m["name"], "size_gb": m.get("size", 0) / 1e9,
             "until": m.get("expires_at", "")}
            for m in data.get("models", [])]


def fits(model: str, host: str = DEFAULT_HOST) -> tuple[bool, str]:
    """Would loading `model` leave the machine usable?"""
    size = next((m["size_gb"] for m in installed(host) if m["name"] == model), None)
    if size is None:
        return False, f"{model} is not installed"
    avail = available_ram_gb()
    if avail is None:
        return True, f"{model} is {size:.1f} GB; available RAM unknown"
    already = any(m["name"] == model for m in resident(host))
    if already:
        return True, f"{model} is already resident ({size:.1f} GB)"
    if size + HEADROOM_GB > avail:
        return False, (
            f"{model} needs {size:.1f} GB and only {avail:.1f} GB is available "
            f"(keeping {HEADROOM_GB:.0f} GB headroom). Unload another model "
            "first -- a model that does not fit is unusable, not merely slow."
        )
    return True, f"{model} ({size:.1f} GB) fits in {avail:.1f} GB available"


def warm(model: str, host: str = DEFAULT_HOST) -> tuple[bool, str]:
    """Load `model` into RAM now, so the next real job is not the one paying.

    An empty prompt makes Ollama load the weights without generating.
    """
    ok, why = fits(model, host)
    if not ok:
        return False, why
    try:
        import time

        t0 = time.perf_counter()
        _post(host, "/api/generate",
              {"model": model, "prompt": "", "keep_alive": KEEP_ALIVE})
        return True, f"{model} resident in {time.perf_counter() - t0:.1f}s ({why})"
    except Exception as exc:  # noqa: BLE001
        return False, f"could not warm {model}: {exc}"


def unload(model: str, host: str = DEFAULT_HOST) -> tuple[bool, str]:
    """Drop `model` from RAM immediately. keep_alive=0 is the documented way."""
    try:
        _post(host, "/api/generate",
              {"model": model, "prompt": "", "keep_alive": 0}, timeout=60)
        return True, f"unloaded {model}"
    except Exception as exc:  # noqa: BLE001
        return False, f"could not unload {model}: {exc}"


def unload_all_except(keep: str | None, host: str = DEFAULT_HOST) -> list[str]:
    """Free every resident model but one.

    Idle models hold their full weight in RAM for nothing, and Ladder normally
    wants exactly one loaded.
    """
    freed = []
    for m in resident(host):
        if m["name"] != keep:
            ok, _ = unload(m["name"], host)
            if ok:
                freed.append(m["name"])
    return freed


def ensure(model: str, host: str = DEFAULT_HOST) -> tuple[bool, str]:
    """Make `model` usable: present on disk, and resident in RAM."""
    if not any(m["name"] == model for m in installed(host)):
        return False, (
            f"{model} is not installed. Run `ollama pull {model}` -- the pull is "
            "large and slow enough that it should be a deliberate act, not "
            "something a tool call does silently."
        )
    if any(m["name"] == model for m in resident(host)):
        return True, f"{model} is already resident"
    return warm(model, host)


def status(host: str = DEFAULT_HOST) -> dict:
    """Everything the report and the MCP tool need in one call."""
    avail = available_ram_gb()
    res = resident(host)
    return {
        "installed": installed(host),
        "resident": res,
        "available_ram_gb": avail,
        "resident_gb": sum(m["size_gb"] for m in res),
        "headroom_gb": HEADROOM_GB,
    }
