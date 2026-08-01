# agent.py

import json
import os
import re
import difflib
import logging
from datetime import datetime
from typing import TypedDict, Dict, Any, Optional, Literal
from langgraph.graph import StateGraph, END
from langchain_groq import ChatGroq
from config import GROQ_API_KEY, MODEL_NAME
from data_loader import (
    load_expense_journal, load_item_category, load_budget,
    load_summary_table, load_summary_panel,
    get_actual_spending, get_expense_records,
)
from data_writer import DataWriter
from sheet_client import SheetClient

logger = logging.getLogger(__name__)

# ── LLM ──────────────────────────────────────────────────────────────────────
try:
    llm = ChatGroq(temperature=0.2, model=MODEL_NAME, api_key=GROQ_API_KEY)
except Exception:
    llm = None


class AgentState(TypedDict):
    user_query: str
    intent: Optional[str]
    parsed_write: Optional[Dict]
    data_context: Dict[str, Any]
    final_answer: str
    pending_question: Optional[str]
    pending_state: Optional[Dict]


def merge_actual_into_budget(actual_spend: dict, budget_dict: dict) -> None:
    """
    Ensure every category in actual_spend has a budget entry, in place.

    FIX (issue #36): category strings aren't case-normalized between the
    "Category Budget" sheet (typed freely by a human) and the "Item &
    Category" sheet (the source of actual_spend's category keys, often
    written by the bot via .title()). A category like "car repair" vs "Car
    Repair" means the exact `cat not in budget_dict` check used to never
    merge them -- silently splitting one real category into two: a
    zero-actual "car repair" budget row and a zero-budget "Car Repair"
    actual-spend row, each looking wrong in a different direction. Try a
    case-insensitive match before defaulting to 0.0. Shared by agent.py and
    bot.py so both startup paths apply the exact same rule instead of two
    independent copies silently drifting apart (the same failure shape this
    whole fix is closing).
    """
    budget_lower = {str(k).lower(): v for k, v in budget_dict.items()}
    for cat in actual_spend:
        if cat not in budget_dict:
            budget_dict[cat] = budget_lower.get(str(cat).lower(), 0.0)


# ── Data refresh ──────────────────────────────────────────────────────────────
def _refresh_data_context(data_context: dict) -> None:
    """
    Refresh all live data from Google Sheets INTO the given dict in-place.

    FIX (issues #2, #3, #7): previous code accepted `state` and did
    ctx = state["data_context"], then refreshed ctx.  But execute_write
    replaced state["data_context"] with pending["data_context"] — which
    could be a *different* dict object from the module-level one stored in
    build_graph's closure.  _refresh then updated the wrong object, leaving
    the module-level dict permanently stale.

    Now callers pass the dict directly so it is always explicit which object
    gets updated.  The module-level dict is always passed.
    """
    sheet_client = data_context.get("sheet_client")
    if not sheet_client:
        return
    try:
        expense_df    = load_expense_journal(sheet_client)
        item_to_cat   = load_item_category(sheet_client)
        budget_dict   = load_budget(sheet_client, "Category Budget")
        summary_table = load_summary_table(sheet_client)
        summary_panel = load_summary_panel(sheet_client)
        actual_spend, total_actual = get_actual_spending(expense_df, item_to_cat)
        # FIX (issue #27): "Next Month Budget" fallback removed — that
        # worksheet no longer exists in the spreadsheet.
        merge_actual_into_budget(actual_spend, budget_dict)
        data_context["actual"]        = actual_spend
        data_context["budget"]        = budget_dict
        data_context["summary_table"] = summary_table
        data_context["summary_panel"] = summary_panel
        data_context["total_actual"]  = total_actual
        data_context["expense_df"]    = expense_df
        data_context["item_to_cat"]   = item_to_cat
    except Exception:
        logger.exception("_refresh_data_context failed")


# ── Category helpers ──────────────────────────────────────────────────────────
def _item_already_mapped(item: str, item_to_cat: dict) -> Optional[str]:
    """
    Return the existing category if this item (case-insensitive) is already
    in the Item & Category sheet, else None.
    """
    item_lower = item.lower().strip()
    for mapped_item, cat in item_to_cat.items():
        if str(mapped_item).lower().strip() == item_lower:
            return cat
    return None


def suggest_category_by_similarity(item: str, existing_categories: list) -> tuple:
    """Suggest a category for an UNKNOWN item using keywords + fuzzy matching."""
    item_lower = item.lower().strip()
    cat_lower_to_original = {c.lower(): c for c in existing_categories}
    keyword_map = {
        'food and grocery': ['apple', 'orange', 'banana', 'mango', 'grapes', 'bread',
                             'milk', 'egg', 'cheese', 'rice', 'pasta', 'vegetable',
                             'fruit', 'avocado', 'tomato', 'potato', 'onion', 'carrot',
                             'coffee', 'tea', 'snack', 'pizza', 'burger'],
        'fruits':        ['apple', 'orange', 'banana', 'mango', 'grapes', 'watermelon', 'pineapple'],
        'commute':       ['bus', 'train', 'taxi', 'uber', 'petrol', 'fuel', 'metro', 'auto'],
        'outing':        ['movie', 'restaurant', 'cafe', 'dinner', 'lunch', 'bar'],
        'grocery':       ['grocery', 'supermarket', 'store', 'produce'],
        'rent':          ['rent', 'lease', 'apartment'],
        'bills':         ['electricity', 'water', 'gas', 'bill', 'broadband', 'internet'],
        'automotive':    ['tyre', 'tire', 'oil', 'service', 'repair'],
        'entertainment': ['music', 'netflix', 'spotify', 'youtube', 'concert', 'game'],
    }
    for target_cat_lower, keywords in keyword_map.items():
        for kw in keywords:
            if kw in item_lower:
                for existing_lower, original in cat_lower_to_original.items():
                    if existing_lower == target_cat_lower:
                        return original, 0.95
                return target_cat_lower.title(), 0.95
    existing_lower_list = list(cat_lower_to_original.keys())
    matches = difflib.get_close_matches(item_lower, existing_lower_list, n=1, cutoff=0.6)
    if matches:
        return cat_lower_to_original[matches[0]], 0.7
    return None, 0.0


def _extract_amount(text: str) -> Optional[float]:
    """
    Find the first numeric amount in free text.

    FIX (issue #30): a bare r'\b(\d+(?:\.\d+)?)\b' search matches only the
    FIRST digit group of a comma-thousands-separated number -- "1,500" or
    "Rs. 12,000" (completely normal Indian-format amounts) matched just "1"
    or "12", silently truncating a real expense by 10x-1000x while still
    reporting a confident success message. Strip commas before searching so
    "1,500" / "12,00,000" are read as single numbers; a comma that instead
    separates two different numbers in the message ("500, 200 for food")
    still resolves correctly since a space remains between the two groups.

    FIX (issue #44): the regex used to have no sign handling at all, so
    "-500" silently became +500 here -- while a negative amount extracted
    directly from the LLM's JSON (a different code path) was preserved
    unchanged and written with no validation. Same conceptual input
    ("log a negative amount"), two different, both-wrong outcomes depending
    on which path parsed it. Now preserves the sign so ALL paths reach
    execute_write's single negative-amount check (issue #44) uniformly.
    """
    m = re.search(r'-?\d+(?:\.\d+)?', text.replace(',', ''))
    return float(m.group(0)) if m else None


def _extract_json(text: str) -> Optional[dict]:
    """
    Extract the outermost JSON object from an LLM response string.

    FIX (issue #5): the previous non-greedy regex `{.*?}` matched the
    INNERMOST braces first.  For `{"notes": "buy {items}"}` it would match
    `{items}` — an invalid JSON fragment — and then fall back to the greedy
    pattern which would also grab everything including trailing garbage.

    Use a simple brace-depth scanner instead: walk the string, count open/close
    braces, and extract exactly the substring from the first `{` to its matching
    closing `}`.  This is O(n) and handles nested braces correctly.

    FIX (issue #13): the depth scanner above still counted braces found INSIDE
    a JSON string value. Braces that happen to be balanced within a string
    (like the docstring's own "buy {items}" example) work by coincidence, but
    an unbalanced brace in a string value — e.g. {"notes": "cost is }100"} —
    made the scanner think the object closed early, producing a truncated,
    invalid JSON fragment. Track whether we're inside a quoted string (and
    whether the current character is escaped) so braces inside strings are
    ignored, matching how a real JSON parser reads structure.
    """
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


# ── Intent classification ─────────────────────────────────────────────────────
def _classify_intent_rule_based(state: AgentState) -> AgentState:
    """
    Keyword/regex fallback classifier. Used only when the LLM is unavailable
    or its classification call fails/returns something unusable — this is the
    pre-NLP behavior, kept as-is so the safety net doesn't drift from what was
    already reviewed and fixed (issues #10 etc. live one layer down in
    execute_write, not here).
    """
    text = state["user_query"].lower().strip()

    query_indicators = [
        "how much", "what is", "how many", "show me", "tell me",
        "summarize", "total", "report", "list", "get", "view",
        "did i spend", "have i spent", "where did i", "when did i",
    ]
    if any(ind in text for ind in query_indicators):
        state["intent"] = "query"
        return state

    write_keywords = [
        "add", "create", "bought", "purchase", "spent on", "paid for",
        "transfer", "withdraw", "deposit",
        # NOTE: "new" removed — too ambiguous ("what is new in my budget")
    ]
    if any(kw in text for kw in write_keywords):
        state["intent"] = "write"
        return state

    if re.search(r'\b\d+\s*(rs|rupees|₹)\b', text, re.IGNORECASE):
        state["intent"] = "write"
        return state

    # FIX (issue #43): a plain expense report with no write verb and no
    # currency suffix -- "500 for groceries today", "300 milk" -- used to
    # fall through to the default "query" here, silently never getting
    # logged whenever this fallback (not the primary LLM path) happens to be
    # active. No query_indicator matched either (checked above), so a bare
    # number at this point is a much stronger write signal than a query
    # signal -- broaden the fallback instead of leaving it undetected.
    if re.search(r'\d', text):
        state["intent"] = "write"
        return state

    state["intent"] = "query"
    return state


def _classify_and_extract_via_llm(user_query: str) -> Optional[dict]:
    """
    Single combined Groq call: classify query-vs-write AND, for write intent,
    extract the add_expense fields in the same response. Replaces the separate
    keyword-list classification + a second LLM call in parse_write_intent with
    one round trip, so real phrasing ("how much have I got left for rent",
    "spent on lunch today") is understood by meaning instead of substring
    matches.

    Returns None (never raises) on ANY failure — network error, malformed
    JSON, missing/invalid "intent" — so the caller can fall back to the
    rule-based classifier without special-casing failure modes.
    """
    now = datetime.now()
    prompt = (
        'You are the intent classifier for a personal finance Telegram bot. '
        'Classify the user message and return ONLY valid JSON.\n\n'
        '"query" = the user is asking about their spending/budget/history '
        '(e.g. "how much did I spend on X", "what\'s left for rent", "show my budget").\n'
        '"write" = the user is reporting a new expense they made, to be logged '
        '(e.g. "add coffee 250", "bought groceries for 500", "paid 100 for lunch").\n\n'
        'If intent is "query", return: {"intent":"query"}\n'
        'If intent is "write", return: {"intent":"write","operation":"add_expense",'
        '"item":string,"amount":number,"day":int,"month":int,"year":int,"notes":string}\n'
        f'Defaults for write if not stated: day={now.day}, month={now.month}, year={now.year}, notes="".\n'
        f'Today: day={now.day}, month={now.month}, year={now.year}.\n'
        f'User message: {user_query!r}\n'
        'Example write: {"intent":"write","operation":"add_expense","item":"Coffee",'
        '"amount":250,"day":15,"month":5,"year":2026,"notes":""}\n'
        'Example query: {"intent":"query"}\n'
        'Now output JSON:'
    )

    try:
        response = llm.invoke(prompt).content.strip()
    except Exception:
        logger.exception("LLM intent-classification call failed")
        return None

    parsed = _extract_json(response)
    if not isinstance(parsed, dict) or parsed.get("intent") not in ("query", "write"):
        return None

    if parsed["intent"] == "query":
        return {"intent": "query"}

    try:
        parsed.setdefault("operation", "add_expense")
        if not parsed.get("item"):
            return None  # no safe way to proceed without an item name
        parsed["item"] = str(parsed["item"])
        amount = parsed.get("amount")
        parsed["amount"] = float(amount) if amount else (_extract_amount(user_query) or 0)
        parsed["day"]    = int(parsed.get("day")   or now.day)
        parsed["month"]  = int(parsed.get("month") or now.month)
        parsed["year"]   = int(parsed.get("year")  or now.year)
        parsed.setdefault("notes", "")
    except (KeyError, TypeError, ValueError):
        return None

    return {"intent": "write", "parsed": parsed}


def classify_intent(state: AgentState) -> AgentState:
    if state.get("pending_state"):
        state["intent"] = "write"
        return state

    text = state["user_query"].lower().strip()

    # Cheap deterministic short-circuit: no need to spend an LLM call on an
    # unambiguous greeting.
    #
    # FIX (issue #31): text.startswith(greetings) matched on PREFIX, so any
    # real expense message that merely *begins* with a greeting word --
    # "Hi, bought coffee for 250", "hey I spent 500 on rent today", "good
    # morning add 300 milk" -- was forced to intent=query and the expense was
    # silently never written, with no error and (since this fires on the very
    # first message) no pending_state to recover from. Only short-circuit
    # when the ENTIRE message (after stripping trivial trailing punctuation)
    # IS the greeting -- anything with real content after it now falls
    # through to the LLM/rule-based classifier instead of being swallowed.
    greetings = {"hi", "hello", "hey", "good morning", "good evening", "how are you"}
    if text.rstrip(" !.?") in greetings:
        state["intent"] = "query"
        return state

    if llm is not None:
        result = _classify_and_extract_via_llm(state["user_query"])
        if result is not None:
            state["intent"] = result["intent"]
            if result["intent"] == "write":
                # Extraction already done in the same LLM call — parse_write_intent
                # will see this is already populated and skip its own LLM call.
                state["parsed_write"] = result["parsed"]
            return state
        logger.warning("LLM intent classification unavailable/invalid; falling back to rule-based classifier")

    return _classify_intent_rule_based(state)


# ── Parse write intent ────────────────────────────────────────────────────────
def parse_write_intent(state: AgentState) -> AgentState:
    if state.get("pending_state"):
        state["parsed_write"] = state["pending_state"].get("parsed", {})
        return state

    if state.get("parsed_write"):
        # classify_intent's combined LLM call already extracted this — don't
        # spend a second round trip re-parsing the same message.
        return state

    if llm is None:
        state["parsed_write"] = {"operation": "unknown"}
        return state

    now = datetime.now()
    prompt = (
        'Extract write operation details from user request. Return ONLY valid JSON.\n'
        'Required fields: operation (must be "add_expense"), item (string), '
        'amount (number), day (int), month (int), year (int), notes (string).\n'
        f'Defaults if missing: day={now.day}, month={now.month}, year={now.year}, notes="".\n'
        f'Today: day={now.day}, month={now.month}, year={now.year}.\n'
        f'User request: {state["user_query"]}\n'
        f'Example: {{"operation":"add_expense","item":"Coffee","amount":250,'
        f'"day":15,"month":5,"year":2026,"notes":"morning coffee"}}\n'
        'Now output JSON:'
    )
    response = llm.invoke(prompt).content.strip()

    parsed = _extract_json(response)  # FIX issue #5: proper brace-depth scanner

    if parsed is None:
        # Full fallback: regex extraction from raw query
        amount = _extract_amount(state["user_query"]) or 0
        item = "Unknown"
        for w in state["user_query"].split():
            if (w.lower() not in {"add", "for", "rs", "rupees", "₹", "on", "at"}
                    and not w.replace('.', '').isdigit()):
                item = w.title()
                break
        state["parsed_write"] = {
            "operation": "add_expense", "item": item, "amount": amount,
            "day": now.day, "month": now.month, "year": now.year, "notes": "",
        }
        return state

    try:
        parsed.setdefault("operation", "add_expense")
        if not parsed.get("amount"):
            parsed["amount"] = _extract_amount(state["user_query"]) or 0
        if not parsed.get("item"):
            for w in state["user_query"].split():
                if (w.lower() not in {"add", "for", "rs", "rupees", "₹", "on", "at"}
                        and not w.replace('.', '').isdigit()):
                    parsed["item"] = w.title()
                    break
            parsed.setdefault("item", "Unknown")
        parsed.setdefault("day",   now.day)
        parsed.setdefault("month", now.month)
        parsed.setdefault("year",  now.year)
        parsed.setdefault("notes", "")
    except (KeyError, TypeError, ValueError):
        parsed = {"operation": "unknown", "error": "parse_failed"}

    state["parsed_write"] = parsed
    return state


def _finalize_expense_write(ctx, writer, item, amount, category, notes, day, month, year, needs_mapping):
    """
    Perform the actual sheet write(s) and report what REALLY happened.

    FIX (issue #28): DataWriter.add_expense/add_category_mapping never raise —
    they catch Sheets API errors internally and return a "❌ Failed..."
    string instead. Every call site used to ignore that return value entirely
    and unconditionally show a success message, so a genuine write failure
    (rate limit, auth hiccup, network blip) was reported to the user as a
    successful save while nothing was actually written. Check the return
    value here and propagate real success/failure to the caller.
    """
    if needs_mapping:
        map_result = writer.add_category_mapping(item, category)
        if map_result.startswith("❌"):
            return False, f"{map_result}\nYour expense was NOT saved — nothing else was written either."
        _refresh_data_context(ctx)

    expense_result = writer.add_expense(year, item, amount, day, month, notes=notes)
    if expense_result.startswith("❌"):
        return False, expense_result
    _refresh_data_context(ctx)
    return True, _success_msg(item, amount, category, notes, day, month, year)


def _retry_pending_state(item, amount, category, notes, day, month, year, needs_mapping):
    """Pending state for a failed write: any reply re-attempts the identical write."""
    return {
        "step": "retry_write",
        "parsed": {"operation": "add_expense"},
        "item": item, "amount": amount, "day": day, "month": month, "year": year,
        "notes": notes, "category": category, "needs_mapping": needs_mapping,
    }


# FIX (issue #32): there was no cancel/escape keyword anywhere. Once
# pending_state is active, classify_intent forces intent="write" unconditionally
# with no exception, so a user had zero way to back out of a multi-turn entry --
# any interrupting message got misread as an answer to whichever step was
# active (at best a confusing re-prompt, at worst -- category_confirm/newcat --
# permanently written to the sheet as a bogus category). Checked before any
# step-specific logic, for every step including a failed-write retry.
CANCEL_KEYWORDS = {"cancel", "stop", "never mind", "nevermind", "quit", "exit", "no thanks"}


# ── Execute write ─────────────────────────────────────────────────────────────
def execute_write(state: AgentState) -> AgentState:  # noqa: C901
    pending = state.get("pending_state")

    # FIX (issues #2, #3, #7): never replace state["data_context"] with the
    # one stored inside pending_state.  The module-level data_context (injected
    # by build_graph's `run` closure) is the canonical, always-live object.
    # Storing it inside pending_state and restoring it later risks diverging
    # object references.  Instead, keep state["data_context"] as-is (the
    # module-level dict) and just pull the other fields from pending.
    if pending:
        step        = pending.get("step")
        user_answer = state["user_query"].strip()

        if user_answer.lower().rstrip(" !.?") in CANCEL_KEYWORDS:
            state["final_answer"]     = "🚫 Cancelled — nothing was saved."
            state["pending_question"] = None
            state["pending_state"]    = None
            return state

        state["parsed_write"]     = pending.get("parsed", {})
        # DO NOT replace state["data_context"] — keep the module-level dict
        state["pending_question"] = None
        state["pending_state"]    = None
    else:
        step        = None
        user_answer = None

    # Always refresh the module-level data_context dict in-place
    _refresh_data_context(state["data_context"])

    parsed = state["parsed_write"] or {}
    ctx    = state["data_context"]
    writer = ctx.get("writer")

    item_to_cat         = ctx.get("item_to_cat", {})
    existing_categories = sorted(set(str(v) for v in item_to_cat.values())) if item_to_cat else []

    if not writer:
        state["final_answer"] = "Writer not initialised."
        return state

    if parsed.get("operation") != "add_expense":
        state["final_answer"] = "Only 'add' is supported. Please say 'add [item] [amount]'."
        return state

    # ── Resolve fields ─────────────────────────────────────────────────────
    now = datetime.now()
    if step is not None:
        item       = pending.get("item",   parsed.get("item", ""))
        amount     = float(pending.get("amount",  parsed.get("amount", 0)))
        day        = int(pending.get("day",    parsed.get("day",    now.day)))
        month      = int(pending.get("month",  parsed.get("month",  now.month)))
        year       = int(pending.get("year",   parsed.get("year",   now.year)))
        user_notes = pending.get("notes",   parsed.get("notes", ""))
    else:
        item       = parsed.get("item", "")
        amount     = float(parsed.get("amount", 0))
        day        = int(parsed.get("day",    now.day))
        month      = int(parsed.get("month",  now.month))
        year       = int(parsed.get("year",   now.year))
        user_notes = parsed.get("notes", "")

    if not item:
        state["final_answer"] = "Could not identify the item name."
        return state
    item = item.title()
    if not user_notes:
        user_notes = f"Added on {now.strftime('%Y-%m-%d %H:%M')}"

    # ── Retry step: a previous write attempt failed, re-attempt it verbatim ─
    if step == "retry_write":
        category      = pending.get("category", "")
        needs_mapping = pending.get("needs_mapping", False)
        success, message = _finalize_expense_write(
            ctx, writer, item, amount, category, user_notes, day, month, year, needs_mapping,
        )
        state["final_answer"] = message
        if success:
            state["pending_question"] = None
            state["pending_state"]    = None
        else:
            state["pending_question"] = f"{message}\nReply anything to try again."
            state["pending_state"] = _retry_pending_state(
                item, amount, category, user_notes, day, month, year, needs_mapping,
            )
        return state

    # ── Step 1: ask for amount if missing ─────────────────────────────────
    if step is None and amount == 0:
        state["pending_question"] = f"💰 How much did you spend on {item}? (e.g., 150)"
        state["pending_state"] = {
            "step": "amount", "parsed": parsed,
            "item": item, "day": day, "month": month, "year": year, "notes": user_notes,
            # FIX: data_context NOT stored in pending — it's always the module-level dict
        }
        return state

    # ── Step 2: received amount ────────────────────────────────────────────
    if step == "amount":
        parsed_amount = _extract_amount(user_answer)  # FIX issue #30: tolerates "1,500" etc.
        if parsed_amount is None:
            # FIX (issue #26): this used to return a bare final_answer with no
            # pending_state — since the top of this function already cleared
            # pending_state/pending_question unconditionally, a non-numeric
            # reply (or an unrelated message interrupting the flow) silently
            # threw away the item/day/month/year the user already provided,
            # forcing them to start the whole entry over. Re-ask for the
            # amount and restore the exact pending_state instead, so the
            # in-progress entry survives an invalid reply.
            state["pending_question"] = (
                f"❌ That doesn't look like an amount. How much did you spend on {item}? (e.g., 150)"
            )
            state["pending_state"] = {
                "step": "amount", "parsed": parsed,
                "item": item, "day": day, "month": month, "year": year, "notes": user_notes,
            }
            return state
        amount = parsed_amount
        # falls through to shared category block below

    # FIX (issue #44): negative amounts used to be handled inconsistently --
    # silently sign-stripped in the OLD amount-step regex but preserved
    # unchecked when extracted straight from the LLM's JSON, with no
    # confirmation either way that a negative ledger entry was intended.
    # This bot has no refund/reversal concept, so the policy is simple:
    # negative amounts aren't a supported expense and are rejected outright,
    # consistently, regardless of which path produced them. Positioned AFTER
    # Step 2 (not right after "Resolve fields") because Step 2 reassigns
    # `amount` from the user's typed reply -- checking any earlier would
    # validate the stale pre-reply value instead of what was actually typed.
    # (amount == 0 is NOT rejected here -- it's the deliberate sentinel
    # meaning "not yet provided", handled by the "ask for amount" step above.)
    if amount < 0:
        state["final_answer"] = (
            f"❌ Amount can't be negative (got {amount:.2f}). Please start over with a positive amount."
        )
        state["pending_question"] = None
        state["pending_state"]    = None
        return state

    # FIX (issue #39): nothing validated day/month/year formed a real
    # calendar date -- month=13, day=45/Feb 30, etc. were written to the
    # sheet verbatim (even echoed back in the success message). Since
    # get_expense_records parses these via pd.to_datetime(errors='coerce')
    # and drops unparseable dates, a bad date used to silently disappear from
    # time-scoped query answers while still counting in all-time totals from
    # get_actual_spending -- an inconsistency invisible until someone asks a
    # time-scoped question. Reject before ever reaching a write.
    try:
        datetime(year, month, day)
    except ValueError:
        state["final_answer"] = (
            f"❌ {day}/{month}/{year} isn't a real date. Please start over with a valid date, "
            f"e.g. 'add {item} {amount if amount else 150} on 15/6/2026'."
        )
        state["pending_question"] = None
        state["pending_state"]    = None
        return state

    # ── Shared block: have item + amount, resolve category ─────────────────
    if step in (None, "amount"):
        existing_cat = _item_already_mapped(item, item_to_cat)
        if existing_cat:
            success, message = _finalize_expense_write(
                ctx, writer, item, amount, existing_cat, user_notes, day, month, year,
                needs_mapping=False,
            )
            state["final_answer"] = message
            if success:
                state["pending_question"] = None
                state["pending_state"]    = None
            else:
                state["pending_question"] = f"{message}\nReply anything to try again."
                state["pending_state"] = _retry_pending_state(
                    item, amount, existing_cat, user_notes, day, month, year, needs_mapping=False,
                )
            return state

        suggested_cat, _ = suggest_category_by_similarity(item, existing_categories)
        if suggested_cat:
            state["pending_question"] = (
                f"📂 Match '{item}' to category '{suggested_cat}'?\n"
                f"Reply y / n, or type a new category name."
            )
            state["pending_state"] = {
                "step": "category_confirm", "parsed": parsed,
                "item": item, "amount": amount, "day": day, "month": month,
                "year": year, "notes": user_notes, "suggested": suggested_cat,
            }
        else:
            state["pending_question"] = f"📂 No category found for '{item}'. Type a category name:"
            state["pending_state"] = {
                "step": "newcat", "parsed": parsed,
                "item": item, "amount": amount, "day": day, "month": month,
                "year": year, "notes": user_notes,
            }
        return state

    # ── Step 3: category confirmation ─────────────────────────────────────
    if step == "category_confirm":
        suggested    = pending.get("suggested", "")
        cat_response = user_answer.strip()

        # FIX (issue #10): `.startswith('y')`/`.startswith('n')` misfired for
        # any real category name beginning with those letters (e.g. "Yoga",
        # "News", "Nutrition") — the user's typed category was silently
        # discarded and treated as a yes/no answer. Only exact y/yes/n/no
        # tokens (case-insensitive) count as confirmation; anything else is
        # taken literally as the category name.
        #
        # FIX (issue #22): the exact-token check above was too narrow — casual
        # replies like "yeah", "yep", "Yes!", "y." fell through to the `else`
        # branch and got saved as the literal category name (e.g. category
        # "Yeah"), which then permanently mis-categorizes that item on every
        # future expense via `_item_already_mapped`. Strip trailing punctuation
        # and match against a small affirmative/negative token set instead of
        # a fixed 4-token list, while still leaving a typed category name
        # (e.g. "Yoga") to fall through to the `else` branch as before.
        cat_response_lower = cat_response.lower().rstrip(" .!?")
        AFFIRMATIVE = {"y", "yes", "yeah", "yep", "ya", "sure", "ok", "okay"}
        NEGATIVE    = {"n", "no", "nope", "nah"}
        if cat_response_lower in AFFIRMATIVE:
            category = suggested
        elif cat_response_lower in NEGATIVE:
            state["pending_question"] = f"📂 Type the category name for '{item}':"
            state["pending_state"] = {
                "step": "newcat", "parsed": parsed,
                "item": item, "amount": amount, "day": day, "month": month,
                "year": year, "notes": user_notes,
            }
            return state
        elif not cat_response:
            # FIX (issue #32): an empty/whitespace-only reply used to fall
            # through to `.title()` ("" -> ""), permanently writing a BLANK
            # category to the sheet. Re-ask instead of accepting nothing.
            state["pending_question"] = (
                f"📂 Match '{item}' to category '{suggested}'?\n"
                f"Reply y / n, or type a category name (or 'cancel')."
            )
            state["pending_state"] = pending
            return state
        else:
            category = cat_response.title()

        # Re-check after refresh to avoid duplicate mapping
        _refresh_data_context(ctx)
        needs_mapping = not _item_already_mapped(item, ctx.get("item_to_cat", {}))
        success, message = _finalize_expense_write(
            ctx, writer, item, amount, category, user_notes, day, month, year, needs_mapping,
        )
        state["final_answer"] = message
        if success:
            state["pending_question"] = None
            state["pending_state"]    = None
        else:
            state["pending_question"] = f"{message}\nReply anything to try again."
            state["pending_state"] = _retry_pending_state(
                item, amount, category, user_notes, day, month, year, needs_mapping,
            )
        return state

    # ── Step 4: new category name ──────────────────────────────────────────
    if step == "newcat":
        # FIX (issue #1): removed dead `cat_response` alias
        category = user_answer.strip().title() if user_answer.strip() else "Miscellaneous"

        _refresh_data_context(ctx)
        needs_mapping = not _item_already_mapped(item, ctx.get("item_to_cat", {}))
        success, message = _finalize_expense_write(
            ctx, writer, item, amount, category, user_notes, day, month, year, needs_mapping,
        )
        state["final_answer"] = message
        if success:
            state["pending_question"] = None
            state["pending_state"]    = None
        else:
            state["pending_question"] = f"{message}\nReply anything to try again."
            state["pending_state"] = _retry_pending_state(
                item, amount, category, user_notes, day, month, year, needs_mapping,
            )
        return state

    state["final_answer"] = "Unexpected state. Please start over."
    return state


# FIX (issue #45): no upper-bound/plausibility check existed anywhere on
# amount -- a fat-finger extra zero ("150000" instead of "1500") was written
# straight to the ledger with no confirmation, the single most common real
# data-entry mistake. A hard block would need per-user/per-category spend
# history to set a sane threshold without false-positiving on genuinely large
# but real expenses (rent, tuition); a soft warning appended to the success
# message needs no such history and still surfaces the "did you mean to add
# an extra zero?" prompt for a human to catch, without blocking the write.
LARGE_AMOUNT_WARNING_THRESHOLD = float(os.getenv("LARGE_AMOUNT_WARNING_THRESHOLD", "100000"))


def _success_msg(item, amount, category, notes, day, month, year) -> str:
    msg = (
        f"✅ Added expense:\n"
        f"📦 Item: {item}\n"
        f"💰 Amount: ₹{amount:.2f}\n"
        f"📂 Category: {category}\n"
        f"📝 Note: {notes}\n"
        f"📅 Date: {day}/{month}/{year}"
    )
    if amount >= LARGE_AMOUNT_WARNING_THRESHOLD:
        msg += f"\n⚠️ That's a large amount (₹{amount:.2f}) — double check it's correct, not a typo."
    return msg


# ── Query answering ───────────────────────────────────────────────────────────
def answer_query_node(state: AgentState) -> AgentState:
    _refresh_data_context(state["data_context"])
    if llm is None:
        state["final_answer"] = "LLM not available."
        return state

    user_q = state["user_query"].lower().strip()
    greetings = {"hi", "hello", "hey", "good morning", "good evening", "greetings", "how are you"}
    # FIX (issue #31, related): this was an exact match with no punctuation
    # stripping, inconsistent with classify_intent's greeting check -- "Hi!"
    # would skip classify_intent's LLM call (correctly) but then still fall
    # through here into a full LLM call just to answer a bare greeting.
    if user_q.rstrip(" !.?") in greetings:
        state["final_answer"] = (
            "Hello! I'm your finance assistant. You can add expenses or ask about "
            "your spending. For example: 'Add coffee 250 rs today' or "
            "'How much did I spend on food?'."
        )
        return state

    actual        = state["data_context"].get("actual", {})
    budget        = state["data_context"].get("budget", {})
    total         = state["data_context"].get("total_actual", 0.0)
    summary_table = state["data_context"].get("summary_table", {})
    summary_panel = state["data_context"].get("summary_panel", {})
    expense_df    = state["data_context"].get("expense_df")
    item_to_cat   = state["data_context"].get("item_to_cat", {})

    lines = [f"Total expenses (live, computed from Expense Journal): ₹{total:.2f}", "\nCategory breakdown (Actual vs Budget):"]
    for cat in sorted(actual.keys()):
        act    = actual.get(cat, 0)
        bud    = budget.get(cat, 0)
        diff   = act - bud
        status = "over" if diff > 0 else "under" if diff < 0 else "on track"
        lines.append(f"  {cat}: ₹{act:.2f} vs ₹{bud:.2f} ({status} by ₹{abs(diff):.2f})")

    # Category-wise totals straight from the sheet's own "Summary Table" pivot
    # (a separate tab from Expense Journal/Category Budget). Shown alongside
    # the computed breakdown above rather than merged into it, since it's a
    # distinct source the user views directly — if it ever disagrees with the
    # computed actuals (e.g. pivot not yet refreshed), that should be visible
    # rather than silently reconciled.
    if summary_table:
        lines.append("\nCategory-wise spending (from the sheet's own Summary Table pivot — "
                     "a secondary cross-check; it may lag the live totals above by one refresh):")
        for cat in sorted(summary_table.keys()):
            lines.append(f"  {cat}: ₹{summary_table[cat]:.2f}")
        lines.append(f"  Summary Table Grand Total: ₹{sum(summary_table.values()):.2f}")

    # Income / Gap / Remaining-at-hand, from the Summary Table sheet's side
    # panel (load_summary_panel). This is the only source in the whole app
    # for income and remaining-cash figures — nothing else tracks income at
    # all — so questions like "what's my income" or "how much do I have
    # left" can only be answered from this block.
    if summary_panel:
        lines.append("\nIncome / Gap / Remaining (from the Summary Table sheet's side panel):")
        for label in ("Income", "Expense", "Gap", "Remaining (At Hand)"):
            if label in summary_panel:
                lines.append(f"  {label}: ₹{summary_panel[label]:.2f}")
    else:
        # Without this, the prompt's instruction below ("use the
        # Income/Gap/Remaining block") would point at data that silently
        # isn't there, and the LLM could guess a plausible-looking income
        # figure instead of saying it doesn't know.
        lines.append("\nIncome/Gap/Remaining data is not available right now.")

    # Record-wise, time-attached data from the Expense Journal, so the LLM
    # can answer time-scoped questions ("last week", "yesterday", "this
    # month") itself instead of only ever seeing the all-time category
    # totals above. Capped to bound prompt size; if the sheet has more rows
    # than the cap, only the most recent are shown and that's called out
    # explicitly so the LLM doesn't silently treat a partial view as complete.
    RECORD_LIMIT  = 300
    all_records   = get_expense_records(expense_df, item_to_cat) if expense_df is not None else []
    records       = all_records[:RECORD_LIMIT]
    if records:
        lines.append(f"\nIndividual expense records, most recent first (date, item, amount, category):")
        if len(all_records) > RECORD_LIMIT:
            lines.append(f"(showing the {RECORD_LIMIT} most recent records; older records exist but aren't shown)")
        for r in records:
            lines.append(f"  {r['date']} | {r['item']} | ₹{r['amount']:.2f} | {r['category']}")

    data_text = "\n".join(lines)
    today_str = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        "Answer this finance question using only the data provided. "
        "If the question is not about finances, ignore the data and respond helpfully.\n"
        f"Today's date is {today_str}.\n"
        "The category breakdown below covers all-time actual-vs-budget spend. "
        "For any overall/total spending question, prefer 'Total expenses' over the "
        "Summary Table's Grand Total — the former is computed live from every "
        "Expense Journal row, the latter is the sheet's own pivot and may lag by "
        "one refresh; only reference the Summary Table block specifically if the "
        "user asks about it directly, or the two disagree and that's worth noting. "
        "For questions about income, the gap between income and spending, or how "
        "much money is remaining/left/at hand, use the Income/Gap/Remaining block if "
        "present below — no other data here covers income. If that block instead says "
        "the data isn't available, say so plainly rather than guessing a number. "
        "The individual records list below it is what you should filter/sum over "
        "yourself for any question scoped to a specific time period (e.g. "
        "'last week', 'yesterday', 'this month').\n"
        f"Data:\n{data_text}\n"
        f"Question: {state['user_query']}\n"
        "Answer:"
    )
    state["final_answer"] = llm.invoke(prompt).content
    return state


# ── Routing & graph ───────────────────────────────────────────────────────────
def route_intent(state: AgentState) -> Literal["write", "query"]:
    return "write" if state["intent"] == "write" else "query"


def build_graph(writer: DataWriter, sheet_client: SheetClient, data_context: dict):
    data_context["writer"]       = writer
    data_context["sheet_client"] = sheet_client

    workflow = StateGraph(AgentState)
    workflow.add_node("classify_intent", classify_intent)
    workflow.add_node("parse_write",     parse_write_intent)
    workflow.add_node("execute_write",   execute_write)
    workflow.add_node("answer_query",    answer_query_node)

    workflow.set_entry_point("classify_intent")
    # FIX (issue #25): route right after classify_intent instead of always
    # running parse_write first — a pure query message no longer triggers a
    # wasted second LLM call in parse_write_intent before being routed away
    # from execute_write.
    workflow.add_conditional_edges(
        "classify_intent", route_intent,
        {"write": "parse_write", "query": "answer_query"},
    )
    workflow.add_edge("parse_write",   "execute_write")
    workflow.add_edge("execute_write", END)
    workflow.add_edge("answer_query",  END)
    compiled = workflow.compile()

    def run(state: AgentState):
        # Always inject the canonical module-level data_context so that
        # every call — fresh or pending — works on the same live dict object.
        state["data_context"] = data_context
        return compiled.invoke(state)

    return run
