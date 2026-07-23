"""tools/acquisition — the ARIA acquisition agent.

Five subsystems (built in milestones — see the build brief):
  M1  ledger + ingest + enroll   (tracking spine; shipped)
  M2  reply agent (draft-only)
  M3  PropStream pull automation
  M4  county research + browsing-fallback enrichment

Prime directive: no number goes in writing before county-record verification.
The agent NEVER auto-sends offers — it drafts, the human approves.
"""
