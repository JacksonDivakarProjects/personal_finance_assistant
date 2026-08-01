---
name: repro-bug
description: Given a reported finance-bot bug, first tries to reproduce it as an automated scenario via bot-scenario-tester, then escalates to the matching auditor only if reproduction confirms it's real.
---

Arguments: a description of the bug/odd behavior a user (or you) observed, e.g. `/repro-bug user replied "Yoga" to a category-confirm question and the bot restarted the flow instead of using it as the category`.

If no description was given, ask the user for the exact sequence of messages and what they expected vs. what happened — do not guess at a plausible-sounding bug and investigate that instead.

## Step 1 — try to reproduce it first, before theorizing

Launch **bot-scenario-tester** (`run_in_background: false`) with a prompt telling it to:
- Construct the exact reported message sequence as a new scenario in its offline test harness (mocked `SheetClient`, mocked/stubbed LLM as needed to match what the report implies the bot said or did).
- Run it and report the actual resulting state (`final_answer`, `pending_question`, `pending_state`, and any calls made to the fake writer) exactly as it came out — not what the auditors' known bug history would predict.
- Keep the scenario in the suite regardless of outcome.

## Step 2 — branch on whether it reproduced

**If it reproduces the reported bad behavior:** it's a real, confirmed bug and now has regression coverage. Launch the matching auditor with the reproduction evidence (the exact scenario, the actual vs. expected output) attached to its prompt, and ask it to pinpoint the exact faulty logic and the minimal fix:
- **conversation-flow-auditor** for anything shaped like a state-machine/step-transition/intent-classification bug (lives in `agent.py`/`bot.py`).
- **sheets-data-integrity-reviewer** for anything shaped like wrong/missing/duplicated data being read from or written to the sheet (lives in `data_loader.py`/`data_writer.py`/`sheet_client.py`).
- If it's genuinely ambiguous which side owns it, run both.

**If it does NOT reproduce:** do not conclude the user was mistaken. Report precisely what the scenario actually produced instead, and ask a clarifying question about what might differ from their real conversation — exact message text (not a paraphrase), whether the bot process restarted between their messages (which would reset `pending_state` if it isn't durably persisted), timing/ordering of messages, or a Telegram-specific detail (e.g. a message edit or a duplicate delivery) the offline harness can't model. Only close this out as "not reproducible" after that clarification, or if the user confirms the paraphrase was exact.

## Step 3 — report

State clearly: the exact reproduction scenario constructed, the actual output observed, confirmed-real or not, and — if confirmed — the file:line root cause and minimal fix from the auditor. Note that the new scenario is now part of the offline suite either way, so `pre-deploy-audit`'s scenario-tester run will include it going forward.
