"""Context offload: let the free model read the archive so the paid one need not.

The problem this solves
-----------------------
A long session spends most of its context window on material it has already
read: chat history, notes, prior decisions. Every token of that is re-sent on
every request. The archive is large, the fraction of it relevant to any one
question is tiny, and the expensive model is the one paying to hold all of it.

So: keep the archive out of the paid context entirely, and ask a free local
model to fetch only the passages that matter.

Why this is dangerous, and what makes it safe
---------------------------------------------
The obvious design -- have the local model read the notes and summarise them --
is the worst possible use of a cheap model, and we have the measurement to prove
it. Asked to document a tool it had no facts about, the local model invented a
command-line syntax and a set of field names, and a paid verifier approved them,
because the verifier had no ground truth either.

A retrieval layer that paraphrases would do exactly that to your own notes, and
the failure would be invisible: the whole point is that the caller no longer has
the source to check against.

So this module never lets the local model *write*. It only lets it **choose**.
Every character returned must appear verbatim in a real file, and that is not a
request in a prompt -- it is checked mechanically, here, against the file on
disk. A quote that does not match is dropped. Fabrication is therefore not
discouraged, it is impossible: the model has no channel through which invented
text could reach the caller.

Selection is the right job for a cheap model anyway. Deciding which of forty
passages bears on a question is a judgement it can make; writing prose that is
true is not.

The degradation path
--------------------
If the local model is unavailable, returns nothing usable, or returns quotes
that fail verification, the caller still gets the search engine's own ranked
hits: paths and the notes' own summary lines. That is exactly what a free
keyword search would have produced, so the tool is never worse than not having
run the model at all.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# The vault and its search index. Overridable so this is not welded to one
# machine, but defaulted so the common case needs no configuration.
VAULT = Path(os.environ.get(
    "LADDER_VAULT",
    r"C:\Users\palencharj\NoOneDrive\MainClaudeMemory\MainClaude"))
SEARCH = Path(os.environ.get(
    "LADDER_VAULT_SEARCH",
    r"C:\Users\palencharj\NoOneDrive\MainClaudeMemory\vault-search\vault_search.py"))

# How many notes the search engine offers before the model filters them.
DEFAULT_CANDIDATES = 8
# Ceiling on what comes back. The entire point is a bounded context cost, so
# this is a hard cap rather than a suggestion.
DEFAULT_MAX_CHARS = 4_000
# Candidate bodies sent to the local model in one go. Local context is cheap
# but not free, and a huge prompt is slow to prefill.
MAX_BODY_CHARS = 24_000

SELECT_SYSTEM = (
    "You are a retrieval filter. You are given a QUESTION and several NOTES.\n\n"
    "Return ONLY a JSON array of objects, each with exactly two keys: "
    '"file" and "quote".\n\n'
    "Rules:\n"
    "- Every quote MUST be copied VERBATIM from the note it cites, character "
    "for character. Do not paraphrase, summarise, correct, shorten, or join "
    "passages. Do not use ellipses.\n"
    "- Quote whole sentences or whole lines. Two to six lines is usually right.\n"
    "- Include a passage only if it genuinely helps answer the question.\n"
    "- If nothing helps, return an empty array [].\n"
    "- Never write a sentence of your own. You are selecting, not writing.\n\n"
    "Return nothing outside the JSON array."
)


def _norm(text: str) -> str:
    """Collapse whitespace for comparison.

    Verification has to tolerate re-wrapping -- a model copying a passage may
    join lines -- without tolerating invented words. Whitespace is the only
    thing normalised; every non-space character must still match.
    """
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class Excerpt:
    """One verbatim passage, and the file it was proved to come from."""

    file: str
    quote: str
    score: float = 0.0

    def as_dict(self) -> dict:
        return {"file": self.file, "quote": self.quote}


@dataclass
class Recall:
    """What came back, and how much it cost to say it."""

    question: str
    excerpts: list[Excerpt] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    rejected: int = 0
    grounded: bool = True
    fell_back: str = ""
    stale_index: str = ""
    seconds: float = 0.0

    @property
    def chars(self) -> int:
        return sum(len(e.quote) for e in self.excerpts)

    def as_dict(self) -> dict:
        return {
            "question": self.question,
            "excerpts": [e.as_dict() for e in self.excerpts],
            "candidates": self.candidates,
            "rejected_unverifiable": self.rejected,
            "fell_back": self.fell_back,
            "stale_index": self.stale_index,
            "chars_returned": self.chars,
            "seconds": round(self.seconds, 1),
        }


def index_is_stale(vault: Path = VAULT, search: Path = SEARCH) -> str:
    """Warn when notes are newer than the search index.

    A silently stale index is the failure mode that makes a recall tool
    untrustworthy in exactly the situation you most need it: right after
    writing the note you are about to ask about. Cheap to check, so check.
    """
    vectors = search.parent / "vault_vectors.npz"
    if not vectors.exists() or not vault.exists():
        return ""
    built = vectors.stat().st_mtime
    newer = [p.name for p in vault.rglob("*.md") if p.stat().st_mtime > built]
    if not newer:
        return ""
    shown = ", ".join(sorted(newer)[:3])
    more = f" (+{len(newer) - 3} more)" if len(newer) > 3 else ""
    return (f"{len(newer)} note(s) changed since the index was built: "
            f"{shown}{more}. Run index_vault.py to include them.")


_HIT = re.compile(r"^\s*([0-9.]+)\s+\[([^\]]+)\]\s+(.+?)\s*$")


def search_vault(question: str, k: int = DEFAULT_CANDIDATES,
                 search: Path = SEARCH) -> list[dict]:
    """Ranked candidate notes. Free -- no model is called."""
    if not search.exists():
        return []
    try:
        proc = subprocess.run(
            [sys.executable, str(search), "-k", str(k), question],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, cwd=str(search.parent),
        )
    except Exception:  # noqa: BLE001
        return []
    if proc.returncode != 0:
        return []

    hits: list[dict] = []
    for line in proc.stdout.splitlines():
        m = _HIT.match(line)
        if m and m.group(3).endswith(".md"):
            hits.append({"score": float(m.group(1)), "kind": m.group(2),
                         "path": m.group(3), "summary": ""})
        elif hits and line.strip() and not m and not hits[-1]["summary"]:
            # The engine prints the note's own summary on the following line.
            hits[-1]["summary"] = line.strip()
    return hits


def read_bodies(hits: list[dict], vault: Path = VAULT,
                budget: int = MAX_BODY_CHARS) -> list[dict]:
    """Load candidate note bodies, newest-ranked first, within a char budget."""
    out, used = [], 0
    for h in hits:
        p = vault / h["path"]
        if not p.is_file():
            continue
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if used + len(body) > budget and out:
            break
        used += len(body)
        out.append({**h, "body": body})
    return out


def verify(selected: list[dict], bodies: list[dict]) -> tuple[list[Excerpt], int]:
    """Keep only quotes that genuinely appear in the file they cite.

    This is the load-bearing function. Everything upstream is a suggestion to a
    cheap model; this is the part that makes the suggestion unnecessary. A quote
    the model invented, altered, or attributed to the wrong file cannot survive
    it, so no invented text can reach the caller.
    """
    index = {b["path"]: _norm(b["body"]) for b in bodies}
    raw = {b["path"]: b["body"] for b in bodies}
    kept: list[Excerpt] = []
    rejected = 0

    for item in selected:
        if not isinstance(item, dict):
            rejected += 1
            continue
        path, quote = item.get("file", ""), item.get("quote", "")
        if not isinstance(path, str) or not isinstance(quote, str):
            rejected += 1
            continue
        # `path` must be non-empty before it reaches the suffix match below:
        # every string ends with "", so an empty path would match the first
        # candidate and the quote would be attributed to a file that never
        # claimed it. Inventing provenance is the same failure as inventing
        # text -- the caller cannot check either.
        if not path.strip() or not quote.strip():
            rejected += 1
            continue
        # Tolerate a model citing a bare filename rather than the vault path.
        if path not in index:
            match = [p for p in index if p.endswith(path) or Path(p).name == path]
            if len(match) != 1:
                rejected += 1
                continue
            path = match[0]
        if _norm(quote) not in index[path]:
            rejected += 1
            continue
        # Return the file's own text, not the model's copy of it, so even
        # whitespace reaches the caller exactly as written.
        kept.append(Excerpt(file=path, quote=_exact_span(raw[path], quote)))
    return kept, rejected


def _exact_span(body: str, quote: str) -> str:
    """The quote as it actually appears in the file, whitespace and all."""
    if quote in body:
        return quote
    # Normalised match: walk the body for the span whose normal form matches.
    target = _norm(quote)
    words = quote.split()
    if not words:
        return quote
    start = body.lower().find(words[0].lower())
    while start != -1:
        for end in range(start + len(target), min(len(body), start + len(target) * 3) + 1):
            if _norm(body[start:end]) == target:
                return body[start:end]
        start = body.lower().find(words[0].lower(), start + 1)
    return quote


def _parse_selection(text: str) -> list[dict] | None:
    """Read the model's JSON array, or None if it cannot be trusted."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```")
                            else lines[1:])
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, list) else None


class Recaller:
    """Answer a question from the archive without spending paid context on it."""

    def __init__(self, router, vault: Path = VAULT, search: Path = SEARCH):
        self.router = router
        self.vault = Path(vault)
        self.search = Path(search)

    def recall(self, question: str, k: int = DEFAULT_CANDIDATES,
               max_chars: int = DEFAULT_MAX_CHARS,
               model: str | None = None) -> Recall:
        t0 = time.perf_counter()
        out = Recall(question=question)
        out.stale_index = index_is_stale(self.vault, self.search)

        hits = search_vault(question, k=k, search=self.search)
        out.candidates = [{key: h[key] for key in ("score", "path", "summary")}
                          for h in hits]
        if not hits:
            out.fell_back = "search returned nothing"
            out.seconds = time.perf_counter() - t0
            return out

        bodies = read_bodies(hits, vault=self.vault)
        if not bodies:
            out.fell_back = "no candidate note could be read"
            out.seconds = time.perf_counter() - t0
            return out

        notes = "\n\n".join(
            f"=== FILE: {b['path']} ===\n{b['body']}" for b in bodies)
        prompt = (f"QUESTION:\n{question}\n\n"
                  f"NOTES:\n{notes}\n\n"
                  "Return the JSON array of verbatim quotes that help answer "
                  "the question.")

        res = self.router.run_job(
            prompt=prompt, kind="extract", rung=0, max_rung=0,
            system_extra=SELECT_SYSTEM, max_tokens=2000,
            title=f"recall: {question[:50]}", model=model,
        )
        if not res.get("ok"):
            out.fell_back = f"local model unavailable ({res.get('error', '')[:80]})"
            out.seconds = time.perf_counter() - t0
            return out

        selected = _parse_selection(res.get("result", ""))
        if selected is None:
            out.fell_back = "model reply was not a usable JSON array"
            out.seconds = time.perf_counter() - t0
            return out

        kept, rejected = verify(selected, bodies)
        out.rejected = rejected
        if not kept:
            out.fell_back = ("nothing the model selected could be verified "
                             "against the source files")
            out.seconds = time.perf_counter() - t0
            return out

        # Enforce the character budget, which is the entire reason this exists.
        budgeted: list[Excerpt] = []
        used = 0
        for e in kept:
            if used + len(e.quote) > max_chars and budgeted:
                break
            budgeted.append(e)
            used += len(e.quote)
        out.excerpts = budgeted
        out.seconds = time.perf_counter() - t0
        return out


def render(out: Recall) -> str:
    """Format for a caller whose context this is supposed to protect."""
    lines: list[str] = []
    if out.stale_index:
        lines.append(f"[stale index] {out.stale_index}")

    if out.excerpts:
        lines.append(
            f"{len(out.excerpts)} verbatim excerpt(s), {out.chars} chars, "
            f"{out.seconds:.0f}s on the free tier. Every quote below was "
            f"checked to appear in the file it cites."
        )
        if out.rejected:
            lines.append(f"({out.rejected} selection(s) dropped: not found in "
                         f"the cited file.)")
        lines.append("")
        for e in out.excerpts:
            lines.append(f"--- {e.file} ---")
            lines.append(e.quote.strip())
            lines.append("")
        return "\n".join(lines)

    # Degraded: hand back the free search's own ranking, which is still useful
    # and costs nothing. Never silently return an empty answer.
    lines.append(f"No verified excerpts ({out.fell_back}). "
                 f"Falling back to the search ranking:")
    lines.append("")
    for c in out.candidates[:8]:
        lines.append(f"  {c['score']:.4f}  {c['path']}")
        if c["summary"]:
            lines.append(f"          {c['summary']}")
    if not out.candidates:
        lines.append("  (no candidates)")
    lines.append("")
    lines.append("Read the files directly if you need more than the summaries.")
    return "\n".join(lines)
