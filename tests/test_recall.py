"""Recall tests.

One property matters more than the rest: **no text the local model invented can
reach the caller.** The caller is offloading context precisely so it no longer
holds the source, which means it cannot spot a fabrication itself. Verification
is therefore not a quality check, it is the whole safety argument, and these
tests attack it directly.

The rest cover graceful degradation. A recall tool that silently returns nothing
is worse than one that hands back a free keyword ranking and says so.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.recall import (  # noqa: E402
    Excerpt,
    Recall,
    Recaller,
    _exact_span,
    _parse_selection,
    index_is_stale,
    render,
    verify,
)

BODY = """---
type: note
summary: A note about rungs.
---
Rung 0 is free and runs on the local box.
A reasoning model emits 10-50x more tokens per answer, so it loses at rung 0.
Never trust rung 0 for code review.
"""

BODIES = [{"path": "notes/Rungs.md", "body": BODY, "score": 0.5, "summary": ""}]


# --------------------------------------------------------------------------
# The safety property
# --------------------------------------------------------------------------

def test_an_invented_quote_is_rejected():
    """The failure this whole design exists to make impossible."""
    kept, rejected = verify(
        [{"file": "notes/Rungs.md",
          "quote": "Rung 0 is excellent for code review."}], BODIES)
    assert kept == []
    assert rejected == 1


def test_a_quote_attributed_to_the_wrong_file_is_rejected():
    """Real text, wrong provenance, is still a false claim about the archive."""
    other = BODIES + [{"path": "notes/Other.md", "body": "Unrelated content.",
                       "score": 0.1, "summary": ""}]
    kept, rejected = verify(
        [{"file": "notes/Other.md", "quote": "Rung 0 is free and runs on the local box."}],
        other)
    assert kept == []
    assert rejected == 1


def test_a_subtly_altered_quote_is_rejected():
    """One changed word is exactly the failure a reader would never notice."""
    kept, rejected = verify(
        [{"file": "notes/Rungs.md",
          "quote": "A reasoning model emits 2-3x more tokens per answer, so it loses at rung 0."}],
        BODIES)
    assert kept == []
    assert rejected == 1


def test_a_genuine_quote_survives():
    kept, rejected = verify(
        [{"file": "notes/Rungs.md", "quote": "Never trust rung 0 for code review."}],
        BODIES)
    assert rejected == 0
    assert len(kept) == 1
    assert kept[0].quote == "Never trust rung 0 for code review."
    assert kept[0].file == "notes/Rungs.md"


def test_rewrapped_whitespace_is_tolerated_but_content_is_not():
    """A model copying a passage may join lines. That must not fail, or the
    tool rejects true quotes and becomes useless -- but only whitespace is
    forgiven, never a changed character."""
    wrapped = "Rung 0 is free and runs\n\n   on the local box."
    kept, rejected = verify(
        [{"file": "notes/Rungs.md", "quote": wrapped}], BODIES)
    assert rejected == 0 and len(kept) == 1
    # Returned in the FILE's own form, not the model's copy of it.
    assert kept[0].quote == "Rung 0 is free and runs on the local box."


def test_a_bare_filename_still_resolves():
    """Citing `Rungs.md` rather than the vault path is a formatting slip, not
    a fabrication, and should not cost a true quote."""
    kept, rejected = verify(
        [{"file": "Rungs.md", "quote": "Never trust rung 0 for code review."}],
        BODIES)
    assert rejected == 0 and kept[0].file == "notes/Rungs.md"


def test_malformed_selections_are_rejected_not_guessed_at():
    kept, rejected = verify(
        [{"file": "notes/Rungs.md"},          # no quote
         {"quote": "Never trust rung 0 for code review."},  # no file
         {"file": "notes/Rungs.md", "quote": "   "},        # blank
         "not even an object",
         {"file": "notes/Nope.md", "quote": "Never trust rung 0 for code review."}],
        BODIES)
    assert kept == []
    assert rejected == 5


def test_exact_span_returns_the_files_own_text():
    body = "alpha  beta\ngamma delta"
    assert _exact_span(body, "beta gamma") == "beta\ngamma"


# --------------------------------------------------------------------------
# Parsing the model's reply
# --------------------------------------------------------------------------

def test_selection_parses_through_a_code_fence():
    got = _parse_selection('```json\n[{"file": "a.md", "quote": "x"}]\n```')
    assert got == [{"file": "a.md", "quote": "x"}]


def test_selection_parses_with_surrounding_prose():
    """Small models preface things. Find the array rather than give up."""
    got = _parse_selection('Here you go:\n[{"file": "a.md", "quote": "x"}]\nHope that helps!')
    assert got == [{"file": "a.md", "quote": "x"}]


def test_an_empty_array_is_a_valid_answer_not_a_failure():
    assert _parse_selection("[]") == []


@pytest.mark.parametrize("bad", ["not json", "", "{}", '{"file": "a"}', "[1,2"])
def test_unusable_replies_return_none(bad):
    assert _parse_selection(bad) is None


# --------------------------------------------------------------------------
# Degradation: never silently return nothing
# --------------------------------------------------------------------------

class FakeRouter:
    def __init__(self, reply="[]", ok=True):
        self.reply, self.ok = reply, ok

    def run_job(self, **kw):
        return {"ok": self.ok, "result": self.reply,
                "error": "" if self.ok else "ollama down", "trail": []}


def _recaller(tmp_path, reply="[]", ok=True):
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "Rungs.md").write_text(BODY, encoding="utf-8")
    r = Recaller(FakeRouter(reply, ok), vault=vault, search=tmp_path / "nope.py")
    return r


def test_a_dead_local_model_falls_back_to_the_search_ranking(tmp_path):
    """The caller still gets paths and summaries -- what a free keyword search
    would have given -- so the tool is never worse than not running it."""
    out = Recall(question="q", candidates=[
        {"score": 0.8, "path": "notes/Rungs.md", "summary": "About rungs."}],
        fell_back="local model unavailable")
    text = render(out)
    assert "notes/Rungs.md" in text
    assert "About rungs." in text
    assert "local model unavailable" in text


def test_missing_search_engine_is_reported_not_crashed(tmp_path):
    out = _recaller(tmp_path).recall("anything")
    assert out.excerpts == []
    assert out.fell_back
    assert "search returned nothing" in out.fell_back


def test_render_marks_excerpts_as_verified():
    out = Recall(question="q", excerpts=[
        Excerpt(file="notes/Rungs.md", quote="Never trust rung 0 for code review.")])
    text = render(out)
    assert "checked to appear in the file it cites" in text
    assert "notes/Rungs.md" in text


def test_stale_index_is_detected(tmp_path):
    """Asking about a note written five minutes ago is exactly when a silently
    stale index makes the tool worthless."""
    import time as _t

    search_dir = tmp_path / "search"
    search_dir.mkdir()
    vectors = search_dir / "vault_vectors.npz"
    vectors.write_text("x", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    _t.sleep(0.02)
    (vault / "Fresh.md").write_text("new", encoding="utf-8")

    msg = index_is_stale(vault, search_dir / "vault_search.py")
    assert "Fresh.md" in msg and "index_vault.py" in msg


def test_no_stale_warning_when_the_index_is_current(tmp_path):
    import time as _t

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Old.md").write_text("old", encoding="utf-8")
    _t.sleep(0.02)
    search_dir = tmp_path / "search"
    search_dir.mkdir()
    (search_dir / "vault_vectors.npz").write_text("x", encoding="utf-8")

    assert index_is_stale(vault, search_dir / "vault_search.py") == ""


def test_an_empty_file_field_cannot_borrow_provenance():
    """Every string ends with "", so a suffix match on an empty path would
    attribute the quote to whichever candidate came first. Inventing
    provenance is the same failure as inventing text: the caller can check
    neither."""
    kept, rejected = verify(
        [{"file": "", "quote": "Never trust rung 0 for code review."}], BODIES)
    assert kept == []
    assert rejected == 1
