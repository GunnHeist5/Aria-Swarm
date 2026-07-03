# LEARNING.md — learn from a link, safely

Send the swarm a YouTube/article link and it distills the takeaway into
something actionable — **without ever letting untrusted web content silently
change its behavior.**

```
learning_ingested {url} → fetch content → distill (LLM) → route (gated):
     ├─ knowledge     → stored in operational_flags["learnings"] (never behavioral)
     ├─ venture_idea  → the normal venture path (autonomy-gated; big bet freezes)
     └─ genome_tweak  → Red Queen must pass it → autonomy gate → adopt or freeze
```

Fire it:
```bash
python main.py --event learning_ingested --payload '{"url": "https://youtu.be/VIDEO_ID"}'
```

## The safety contract (why this can't be prompt-injected)

A link is **untrusted input** — a video description could try to steer the
swarm. So the pipeline separates *ingesting* (open) from *adopting* (gated):

- The distiller is told the text is untrusted and must not follow instructions
  inside it. Anything unparseable degrades to `knowledge` (the inert bucket).
- **Knowledge** is only ever stored — it changes no behavior.
- A **venture idea** enters the exact same `propose_venture` path as any other
  venture, so the graduated-autonomy gate applies (a big bet freezes for you).
- A **genome tweak** (changing an agent's prompt) must:
  1. **survive the Red Queen** (`sandbox.adversarial_evaluate` ≥ stability
     threshold) — a weak mutation is rejected and the live genome is untouched;
  2. then clear the **autonomy gate** (`resolve_autonomy`) — a *structural /
     massive* change, or one that would cost a big % of treasury, escalates to
     **you** (freeze `learning_adopt_gate:<role>`); only a small, vetted tweak
     hot-swaps into the live genome automatically.

So the rule you set holds: **Red Queen approval for ordinary changes, human
approval for anything big.** Rejected/gated candidates are parked in
`operational_flags["pending_learnings"]` for review.

## Live fetching (VPS)

The content fetcher is a seam. Offline it uses a stub (no network); the live
YouTube-transcript / article extractors are VPS adapters injected behind
`tools/learn/fetch.fetch_content(url, fetcher=...)`. They need network and
return untrusted text — which is exactly why everything downstream stays gated.

## Liquid AI backend

Separately, Liquid's efficient LFMs are wired as a model backend
(`liquid-lfm`). Set `LIQUID_API_KEY` in the VPS `.env` and point Saving Mode at
it with `SAVING_MODE_MODEL=liquid-lfm` to run the cheap worker tier (and the
distiller above) on Liquid — the endothermy move: less dependent on volatile
public API weather. Confirm `LIQUID_BASE_URL`/`LIQUID_MODEL_SLUG` for your
account on the box.
