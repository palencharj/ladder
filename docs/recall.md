# Context offload

`ladder_recall` answers a question from the memory vault **without loading the
vault into the paid model's context**. The free local model reads the archive;
only the passages that matter come back.

## The problem

A long session spends most of its context on material it has already read —
notes, prior decisions, chat history — and every token of it is re-sent on every
request. The archive is large, the fraction relevant to any one question is
tiny, and the expensive model is the one paying to hold all of it.

## Why the obvious design is dangerous

The instinct is to have the local model *read the notes and summarise them*.
That is the worst possible use of a cheap model, and this repo has the
measurement: asked to document a tool it had no facts about, the local model
invented a command-line syntax and a set of field names, and a **paid verifier
approved them** — because the verifier had no ground truth either.

Point that at your own notes and the failure becomes invisible. The whole reason
you called the tool is that you no longer hold the source, so a confident
misreport of what a note says is undetectable.

## What makes it safe

**The local model never writes. It only chooses.**

Every character returned must appear verbatim in a real file, and that is not a
request in a prompt — it is checked mechanically in `verify()`, against the file
on disk. A quote that does not match is dropped before the caller sees it.

Fabrication is therefore not discouraged; it is **impossible**. There is no
channel through which invented text could reach you. The model can only choose
badly, and a bad choice is visible to you as an irrelevant quote.

That inverts the usual RAG instinct, and it is what makes a cheap model
appropriate here. Deciding which of forty passages bears on a question is a
judgement it can make. Writing prose that is true is not.

Verification is strict about content and forgiving about whitespace — a model
copying a passage may re-wrap it — and the text returned is **the file's own**,
not the model's copy of it.

### Provenance is checked too

An early version accepted a quote whose `file` field was empty, because
`"anything".endswith("")` is `True` — so the passage was silently attributed to
whichever candidate happened to come first. Real text, invented provenance. The
caller can check neither, so both are rejected now.

## Measured

Question: *"why was nemotron rejected for rung 0"*, against the real vault.

| draft model | time | verified excerpts | chars | quality |
|---|---|---|---|---|
| qwen3-coder:30b | 250 s | 3 | 1,033 | answered the question directly |
| qwen2.5-coder:7b | 219 s | 2 | 466 | on topic, thinner |
| qwen2.5-coder:3b | 87 s | 1 | 99 | tangential |

Nothing failed verification in any run — the models were selecting honestly,
just better or worse. **The 30B is worth its time**; the 3B returns a fraction
of the useful material.

For comparison, the vault's own `Home.md` records that browsing Home plus a map
costs **~2,871 tokens** against **~377** for a search. This adds the filtering
step on top of that, at zero allowance.

**The honest cost is latency, not tokens.** 250 s is for questions worth waiting
on, not a reflex on every turn. Most of it is prefill of the candidate note
bodies.

## Degradation

If the local model is unavailable, replies unusably, or selects nothing that
verifies, you still get the search engine's own ranked paths and summary lines,
clearly marked as a fallback. That is exactly what a free keyword search would
have produced, so the tool is never worse than not having run it.

## Stale index

The search index is a built artifact. Ask about a note written five minutes ago
and a silently stale index gives you nothing — the worst moment for it to fail
quietly. `ladder_recall` compares note mtimes against the index and says so:

```
[stale index] 9 note(s) changed since the index was built: 2026-09-03.md,
Home.md (+7 more). Run index_vault.py to include them.
```

## Using it

```
ladder_recall(question="why was nemotron rejected for rung 0")
```

| Parameter | Type | Meaning |
|---|---|---|
| `question` | string | A natural question beats keywords; retrieval is hybrid semantic + lexical |
| `k` | integer | Candidate notes considered before filtering. Default 8 |
| `max_chars` | integer | Hard cap on returned text. Default 4000 — the bounded context cost is the point |
| `model` | string | Local model override for the selection step |

Configure the paths with `LADDER_VAULT` and `LADDER_VAULT_SEARCH` if your vault
lives elsewhere.
