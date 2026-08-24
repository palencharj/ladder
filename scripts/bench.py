"""Measure what rung 0 actually does on THIS machine.

Local throughput varies enormously by hardware, and the honest answer decides
how you use the tool. On a workstation with a discrete GPU, rung 0 may be fast
enough for interactive work. On a thin-and-light laptop it will not be, and
you need to know that before you build a workflow around it.

Reports prompt-processing and generation rates separately, because they scale
differently: prefill is compute-bound, generation is memory-bandwidth-bound.

    python scripts/bench.py
    python scripts/bench.py --models qwen3-coder:30b qwen2.5-coder:7b
    python scripts/bench.py --concurrency 1 2 4
"""

from __future__ import annotations

import argparse
import ast
import json
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.engines.ollama_engine import DEFAULT_HOST  # noqa: E402

PROMPT = (
    "Write a Python function `retry(fn, attempts=3, delay=0.5)` that retries a "
    "callable on exception with exponential backoff. Include a docstring."
)
SYSTEM = "You are a terse Python coder. Output only code."


def one_call(host: str, model: str, num_predict: int) -> dict:
    payload = {
        "model": model, "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": PROMPT},
        ],
        "options": {"num_predict": num_predict, "temperature": 0.2},
    }
    req = urllib.request.Request(
        f"{host}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=1800) as r:
        data = json.loads(r.read().decode())
    wall = time.perf_counter() - t0

    text = (data.get("message") or {}).get("content", "")
    code = text
    if code.strip().startswith("```"):
        code = "\n".join(code.strip().split("\n")[1:-1])
    try:
        ast.parse(code)
        valid = True
    except SyntaxError:
        valid = False

    gen_s = max(data.get("eval_duration", 1) / 1e9, 1e-9)
    pre_s = max(data.get("prompt_eval_duration", 1) / 1e9, 1e-9)
    return {
        "wall": wall,
        "out_tokens": data.get("eval_count", 0),
        "in_tokens": data.get("prompt_eval_count", 0),
        "gen_tps": data.get("eval_count", 0) / gen_s,
        "prefill_tps": data.get("prompt_eval_count", 0) / pre_s,
        "valid_python": valid,
    }


def installed(host: str) -> list[str]:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as r:
            return [m["name"] for m in json.loads(r.read().decode())["models"]]
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot reach Ollama at {host}: {exc}", file=sys.stderr)
        return []


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--models", nargs="*", default=None,
                    help="Models to test. Default: everything installed.")
    ap.add_argument("--runs", type=int, default=2, help="Runs per model.")
    ap.add_argument("--tokens", type=int, default=300, help="Max tokens to generate.")
    ap.add_argument("--concurrency", type=int, nargs="*", default=[1],
                    help="Parallel request counts to test. Try: 1 2 4")
    args = ap.parse_args()

    have = installed(args.host)
    if not have:
        sys.exit("No local models found. Try: ollama pull qwen3-coder:30b")
    models = args.models or have
    missing = [m for m in models if m not in have]
    if missing:
        print(f"Not installed, skipping: {', '.join(missing)}", file=sys.stderr)
    models = [m for m in models if m in have]

    print(f"Ollama at {args.host}")
    print(f"{len(models)} model(s), {args.runs} run(s) each, {args.tokens} max tokens\n")

    for model in models:
        print(f"--- {model} ---")
        for conc in args.concurrency:
            samples = []
            for _ in range(args.runs):
                if conc == 1:
                    samples.append(one_call(args.host, model, args.tokens))
                else:
                    t0 = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=conc) as pool:
                        batch = list(pool.map(
                            lambda _, m=model: one_call(args.host, m, args.tokens),
                            range(conc),
                        ))
                    span = time.perf_counter() - t0
                    total_out = sum(b["out_tokens"] for b in batch)
                    samples.append({
                        "wall": span,
                        "out_tokens": total_out,
                        "in_tokens": sum(b["in_tokens"] for b in batch),
                        "gen_tps": total_out / span,  # aggregate throughput
                        "prefill_tps": statistics.mean(b["prefill_tps"] for b in batch),
                        "valid_python": all(b["valid_python"] for b in batch),
                    })

            gen = statistics.mean(s["gen_tps"] for s in samples)
            pre = statistics.mean(s["prefill_tps"] for s in samples)
            wall = statistics.mean(s["wall"] for s in samples)
            valid = sum(s["valid_python"] for s in samples)
            label = "serial" if conc == 1 else f"{conc}-way aggregate"
            print(
                f"  {label:<18} gen={gen:6.1f} tok/s  prefill={pre:7.1f} tok/s  "
                f"wall={wall:6.1f}s  syntax-ok {valid}/{len(samples)}"
            )
        print()

    print("Reading the numbers:")
    print("  Under ~10 tok/s, rung 0 is batch-only -- do not put it in a loop")
    print("  a human is waiting on. It is still free, so it stays worthwhile")
    print("  for bulk mechanical work you can walk away from.")
    print("  If N-way aggregate throughput is no better than serial, the box is")
    print("  memory-bandwidth-bound and local concurrency buys you nothing;")
    print("  keep the rung-0 concurrency cap at 1-2.")


if __name__ == "__main__":
    main()
