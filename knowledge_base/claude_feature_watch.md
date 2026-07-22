# Claude Feature Watch — ledger

Maintained by the weekly feature-scout Routine. Every Anthropic/Claude feature
that has been assessed for swarm use gets a row here — including "no use"
verdicts — so no feature is ever assessed twice. New features found by the
scout are appended with an assessment and either (a) an in-repo integration
built on the spot, or (b) a business blueprint awaiting Justin's approval.

Rules the scout must honor (mirrors CLAUDE.md hard constraints):
- Build only safe, in-repo code/docs. Tests green before push.
- Push ONLY to `claude/swarm-architecture-frozen-z6ac7j`.
- NEVER spend money, create external accounts, touch wallet/keys, or alter
  live outreach. Business launches are blueprints for Justin, not actions.

## Baseline (assessed 2026-07-22 — do not re-assess)

| Feature | Verdict | Notes |
|---|---|---|
| Claude 5 family (Fable 5) / Opus 4.8 / Sonnet 5 / Haiku 4.5 | IN USE | Model routing already a swarm concept (hybrid-LLM router). |
| Claude Code (CLI, web, desktop, IDE) | IN USE | This repo is built and operated through it. |
| Claude Agent SDK | ASSESSED | Candidate runtime if swarm agents ever move off LangGraph; no action now. |
| MCP servers/connectors (Gmail, Calendar, Drive, GitHub) | PARTIAL | GitHub in use. Gmail/Calendar could power seller-reply triage + call scheduling — candidate integration. |
| Skills / plugins / slash commands | IN USE | Session tooling; not a swarm runtime component. |
| Workflows (multi-agent orchestration) | IN USE | Used for build/review passes on this repo. |
| Artifacts + runtime capabilities (publish live web pages/apps) | OPPORTUNITY | "Website builder": publish a buyer-facing dispo site — live inventory of contracts for assignment (property, price, photos, buy-now contact). Feeds the dispo side of every deal. Blueprint below. |
| Claude in Chrome (browser control on Justin's machine) | IN USE | Used for PandaDoc template field placement; general UI-task escape hatch. |
| Computer use API | ASSESSED | VPS browser needs are covered by the Playwright runner; revisit if a Web2 surface resists scripting. |
| Higgsfield MCP (image/video/websites via session connector) | OPPORTUNITY | Marketing assets: parcel promo images/videos for dispo listings and buyer emails. Session-connector only (not on VPS) — usable when Justin runs sessions here. |

## Opportunity blueprints (awaiting Justin's go)

### 1. Dispo website — live "land for sale" inventory (Artifacts or static site)
- **What**: one page per contracted property (address, acreage, flood/frontage
  facts from the screener, asking price, assignment terms) + an index page.
  Auto-generated from the deal desk data the swarm already keeps.
- **First dollar**: faster assignment of the FIRST closed contract — buyers
  see inventory without Justin manually blasting PDFs.
- **Needs from Justin**: choice of host (Artifact page vs. a real domain),
  and go-ahead once there's ≥1 property under contract to list.
- **Cost**: ~$0 (artifact) or domain cost only.

## Scout log

(append newest first: date — features found — actions taken)
