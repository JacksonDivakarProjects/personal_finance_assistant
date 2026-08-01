# agent.py

import json
import re
import difflib
import logging
from datetime import datetime
from typing import TypedDict, Dict, Any, Optional, Literal
from langgraph.graph import StateGraph, END
from langchain_groq import ChatGroq
from config import GROQ_API_KEY, MODEL_NAME
from data_loader import load_expense_journal, load_item_category, load_budget, get_actual_spending
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
        expense_df       = load_expense_journal(sheet_client)
        item_to_cat      = load_item_category(sheet_client)
        budget_dict      = load_budget(sheet_client, "Category Budget")
        next_budget_dict = load_budget(sheet_client, "Next Month Budget")
        actual_spend, total_actual = get_actual_spending(expense_df, item_to_cat)
        for cat in actual_spend:
            if cat not in budget_dict and cat in next_budget_dict:
                budget_dict[cat] = next_budget_dict[cat]
            elif cat not in budget_dict:
                budget_dict[cat] = 0.0
        data_context["actual"]       = actual_spend
        data_context["budget"]       = budget_dict
        data_context["total_actual"] = total_actual
        data_context["expense_df"]   = expense_df
        data_context["item_to_cat"]  = item_to_cat
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
def classify_intent(state: AgentState) -> AgentState:
    if state.get("pending_state"):
        state["intent"] = "write"
        return state

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

    greetings = ["hi", "hello", "hey", "good morning", "good evening", "how are you"]
    if text in greetings or text.startswith(tuple(greetings)):
        state["intent"] = "query"
        return state

    state["intent"] = "query"
    return state


# ── Parse write intent ────────────────────────────────────────────────────────
def parse_write_intent(state: AgentState) -> AgentState:
    if state.get("pending_state"):
        state["parsed_write"] = state["pending_state"].get("parsed", {})
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
        amount_match = re.search(r'\b(\d+(?:\.\d+)?)\b', state["user_query"])
        amount = float(amount_match.group(1)) if amount_match else 0
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
            m = re.search(r'\b(\d+(?:\.\d+)?)\b', state["user_query"])
            parsed["amount"] = float(m.group(1)) if m else 0
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
        m = re.search(r'\b(\d+(?:\.\d+)?)\b', user_answer)
        if not m:
            state["final_answer"] = "❌ Invalid amount. Please send a number, e.g., '150'."
            return state
        amount = float(m.group(1))
        # falls through to shared category block below

    # ── Shared block: have item + amount, resolve category ─────────────────
    if step in (None, "amount"):
        existing_cat = _item_already_mapped(item, item_to_cat)
        if existing_cat:
            writer.add_expense(year, item, amount, day, month, notes=user_notes)
            _refresh_data_context(ctx)
            state["final_answer"]     = _success_msg(item, amount, existing_cat, user_notes, day, month, year)
            state["pending_question"] = None
            state["pending_state"]    = None
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
        else:
            category = cat_response.title()

        # Re-check after refresh to avoid duplicate mapping
        _refresh_data_context(ctx)
        if not _item_already_mapped(item, ctx.get("item_to_cat", {})):
            writer.add_category_mapping(item, category)
            _refresh_data_context(ctx)

        writer.add_expense(year, item, amount, day, month, notes=user_notes)
        _refresh_data_context(ctx)
        state["final_answer"]     = _success_msg(item, amount, category, user_notes, day, month, year)
        state["pending_question"] = None
        state["pending_state"]    = None
        return state

    # ── Step 4: new category name ──────────────────────────────────────────
    if step == "newcat":
        # FIX (issue #1): removed dead `cat_response` alias
        category = user_answer.strip().title() if user_answer.strip() else "Miscellaneous"

        _refresh_data_context(ctx)
        if not _item_already_mapped(item, ctx.get("item_to_cat", {})):
            writer.add_category_mapping(item, category)
            _refresh_data_context(ctx)

        writer.add_expense(year, item, amount, day, month, notes=user_notes)
        _refresh_data_context(ctx)
        state["final_answer"]     = _success_msg(item, amount, category, user_notes, day, month, year)
        state["pending_question"] = None
        state["pending_state"]    = None
        return state

    state["final_answer"] = "Unexpected state. Please start over."
    return state


def _success_msg(item, amount, category, notes, day, month, year) -> str:
    return (
        f"✅ Added expense:\n"
        f"📦 Item: {item}\n"
        f"💰 Amount: ₹{amount:.2f}\n"
        f"📂 Category: {category}\n"
        f"📝 Note: {notes}\n"
        f"📅 Date: {day}/{month}/{year}"
    )


# ── Query answering ───────────────────────────────────────────────────────────
def answer_query_node(state: AgentState) -> AgentState:
    _refresh_data_context(state["data_context"])
    if llm is None:
        state["final_answer"] = "LLM not available."
        return state

    user_q = state["user_query"].lower().strip()
    greetings = {"hi", "hello", "hey", "good morning", "good evening", "greetings", "how are you"}
    if user_q in greetings:
        state["final_answer"] = (
            "Hello! I'm your finance assistant. You can add expenses or ask about "
            "your spending. For example: 'Add coffee 250 rs today' or "
            "'How much did I spend on food?'."
        )
        return state

    actual = state["data_context"].get("actual", {})
    budget = state["data_context"].get("budget", {})
    total  = state["data_context"].get("total_actual", 0.0)

    lines = [f"Total expenses: ₹{total:.2f}", "\nCategory breakdown (Actual vs Budget):"]
    for cat in sorted(actual.keys()):
        act    = actual.get(cat, 0)
        bud    = budget.get(cat, 0)
        diff   = act - bud
        status = "over" if diff > 0 else "under" if diff < 0 else "on track"
        lines.append(f"  {cat}: ₹{act:.2f} vs ₹{bud:.2f} ({status} by ₹{abs(diff):.2f})")

    data_text = "\n".join(lines)
    prompt = (
        "Answer this finance question using only the data provided. "
        "If the question is not about finances, ignore the data and respond helpfully.\n"
        f"Data:\n{data_text}\n"
        f"Question: {state['user_query']}\n"
        "Answer:"
    )
    state["final_answer"] = llm.invoke(prompt).content
    return state


# ── Routing & graph ───────────────────────────────────────────────────────────
def route_intent(state: AgentState) -> Literal["execute_write", "answer_query"]:
    return "execute_write" if state["intent"] == "write" else "answer_query"


def build_graph(writer: DataWriter, sheet_client: SheetClient, data_context: dict):
    data_context["writer"]       = writer
    data_context["sheet_client"] = sheet_client

    workflow = StateGraph(AgentState)
    workflow.add_node("classify_intent", classify_intent)
    workflow.add_node("parse_write",     parse_write_intent)
    workflow.add_node("execute_write",   execute_write)
    workflow.add_node("answer_query",    answer_query_node)

    workflow.set_entry_point("classify_intent")
    workflow.add_edge("classify_intent", "parse_write")
    workflow.add_conditional_edges(
        "parse_write", route_intent,
        {"execute_write": "execute_write", "answer_query": "answer_query"},
    )
    workflow.add_edge("execute_write", END)
    workflow.add_edge("answer_query",  END)
    compiled = workflow.compile()

    def run(state: AgentState):
        # Always inject the canonical module-level data_context so that
        # every call — fresh or pending — works on the same live dict object.
        state["data_context"] = data_context
        return compiled.invoke(state)

    return run
