<!--
Opener variants — an A/B test, kept as discrete named variants (SPEC §11 TODO
(a)). Each `## <variant>` section below is one variant; the section body is
the exact spoken opener. agent/script.py parses these headings, strips HTML
comments, substitutes {{CompanyName}}, and tracks performance per variant via
call_attempts.opener_variant, so keep variant names stable and in sync with
cfg.opener_variants.

Rules for every variant, no exceptions:
- It MUST disclose the AI: "an AI assistant calling on behalf of Justin at
  Reachwell". The disclosure is a compliance hard requirement (SPEC §6), not
  copy — do not trim it while editing.
- It must end by asking what happens to calls they miss, then STOP (the agent
  waits for the prospect's response per CALL FLOW step 1).

TODO: both bodies below are placeholder copy awaiting Justin's real A/B
variants from the JustCall knowledge base. Replace the prose, keep the
headings and the AI disclosure.
-->

## opener_a

<!-- TODO: replace with Justin's real "A" opener copy. -->
Hi, this is an AI assistant calling on behalf of Justin at Reachwell — am I
speaking with the owner of {{CompanyName}}? The reason for the quick call:
when you're out on a job and can't pick up, what happens to the calls you
miss?

## opener_b

<!-- TODO: replace with Justin's real "B" opener copy. -->
Hi there — quick heads-up, I'm an AI assistant calling on behalf of Justin at
Reachwell. Do I have the owner of {{CompanyName}}? Justin asked me to reach
out because most service businesses lose jobs to missed calls — when a call
comes in that you can't answer, what happens to it today?
