---
name: fix-check
description: Fast feedback after editing this finance bot's code — runs only the review agent(s) matching the current diff instead of a full audit, for quick iteration while fixing a bug.
---

Use this mid-session, right after making a change, before committing — a quick targeted check, not a full release audit (use `pre-deploy-audit` for that before an actual deploy).

## Step 1 — see what actually changed

Run `git status` and `git diff` (against HEAD if there are uncommitted changes; otherwise `git diff HEAD~1 HEAD` for the most recent commit) to get the real set of changed files and line ranges. Don't guess from conversation context what was touched — confirm it.

## Step 2 — map changed files to the matching agent(s)

- `agent.py` or `bot.py` changed → **conversation-flow-auditor**
- `data_loader.py`, `data_writer.py`, or `sheet_client.py` changed → **sheets-data-integrity-reviewer**
- Both groups changed → launch both in parallel, single message, `run_in_background: false`.
- Neither group changed (e.g. only `config.py`, `app.py`, `requirements.txt`, docs) → say plainly that nothing in this diff falls under either agent's scope, and stop here. Do not invent relevance to justify running an agent anyway.

## Step 3 — scope the agent to the actual change

Pass the real diff (or a precise summary of which functions/lines changed) into each agent's prompt, and tell it explicitly to focus its adversarial review on the changed lines first, while still reading surrounding code for context it needs (state shapes, call sites elsewhere in the file that the change could affect). A diff-blind full-file review here defeats the point of this being the fast path.

## Step 4 — scenario tests are opt-in here, not automatic

Do NOT invoke **bot-scenario-tester** as part of this skill by default — it builds/runs the full offline suite and is meant for `pre-deploy-audit`. Only invoke it here if the user explicitly asks in the same request, or the diff itself touches files under `tests/`.

## Step 5 — report

Return the agent's (or both agents') findings directly. No verdict synthesis needed — this is a quick pass, not a gate. If both agents ran, keep their findings clearly separated by file/scope rather than merging them.
