---
name: pre-deploy-audit
description: Full production-readiness audit before deploying the finance bot — runs conversation-flow-auditor, sheets-data-integrity-reviewer, and bot-scenario-tester, then synthesizes one ranked go/no-go verdict by real-money impact.
---

Run this before any deploy, release, or merge to main of this finance bot. The app writes a real user's real money into a live Google Sheet with no backup path — treat this as a release gate, not a formality. Do not shorten or skip steps to save time.

## Step 1 — launch all three subagents in parallel

In a single message, call the Agent tool three times (`run_in_background: false` on each, since Step 2 needs all three results together):

1. **conversation-flow-auditor** — prompt it to audit `agent.py`'s conversation state machine (`classify_intent`, `parse_write_intent`, `execute_write`) for production-impacting bugs ahead of this deploy. Tell it explicitly: assume something is broken until proven otherwise; use its full adversarial method; report in its standard format.
2. **sheets-data-integrity-reviewer** — prompt it to audit `sheet_client.py`, `data_loader.py`, `data_writer.py` for data-corruption risk ahead of this deploy, using its standard adversarial method and output format.
3. **bot-scenario-tester** — prompt it to build/extend and actually run the offline scenario suite (all standard scenarios: happy path, missing amount, ambiguous category confirm, unknown item/no suggestion, interrupted flow, malformed amount reply, duplicate-case item, LLM-garbage fallback, query-intent regressions), and report pass/fail with evidence per its standard output format. Remind it: never touch the live sheet or a real API key, stdlib only.

## Step 2 — synthesize, don't just concatenate

Once all three return, build ONE combined verdict:

- Pull every finding from both auditors and sort by real-money impact using their own severity ordering: money written wrong/twice > conversation gets stuck > silent data loss > misclassified intent > cosmetic.
- Cross-reference each finding against the scenario-tester's results: does an existing (or newly added) test actually exercise this exact code path? If yes, note "covered — test X currently passes/fails." If no, explicitly flag it as "no regression coverage" — an uncovered finding is a bigger risk than a covered one even at the same severity, because it can silently regress again later with nothing to catch it.
- Do not drop or water down a finding just because fixing it would take longer — surface everything the subagents reported, ranked, even low-severity ones (list them at the bottom, clearly separated from anything release-blocking).

## Step 3 — verdict

State one of:
- **GO** — no open money-written-wrong/twice or silent-data-loss findings, and the scenario suite passes in full.
- **GO WITH CAVEATS** — only lower-severity or well-understood findings remain, name them explicitly and why they're acceptable to ship with.
- **NO-GO** — any unresolved money-written-wrong/twice or silent-data-loss finding, OR any scenario-suite failure. Default to NO-GO when in doubt; do not round up to GO to be agreeable. State exactly what must be fixed to flip the verdict.

Report the full ranked finding list plus the verdict — never the verdict alone.
