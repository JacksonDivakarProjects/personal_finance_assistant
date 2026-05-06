# agent.py (no update feature – only add expense and query)

import os
import json
import re
import difflib
from datetime import datetime
from typing import TypedDict, Dict, Any, Optional, Literal
from langgraph.graph import StateGraph, END
from langchain_groq import ChatGroq
from config import GROQ_API_KEY, MODEL_NAME
from data_loader import load_expense_journal, load_item_category, load_budget, get_actual_spending
from data_writer import DataWriter
from sheet_client import SheetClient

# ----------------------------------------------------------------------
# LLM only for answer generation and parsing
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# Helper: refresh data from sheets
# ----------------------------------------------------------------------
def _refresh_data_context(state: AgentState):
    ctx = state["data_context"]
    sheet_client = ctx.get("sheet_client")
    if not sheet_client:
        return
    try:
        expense_df = load_expense_journal(sheet_client)
        item_to_cat = load_item_category(sheet_client)
        budget_dict = load_budget(sheet_client, "Category Budget")
        next_budget_dict = load_budget(sheet_client, "Next Month Budget")
        actual_spend, total_actual = get_actual_spending(expense_df, item_to_cat)
        for cat in actual_spend:
            if cat not in budget_dict and cat in next_budget_dict:
                budget_dict[cat] = next_budget_dict[cat]
            elif cat not in budget_dict:
                budget_dict[cat] = 0.0
        ctx["actual"] = actual_spend
        ctx["budget"] = budget_dict
        ctx["total_actual"] = total_actual
        ctx["expense_df"] = expense_df
        ctx["item_to_cat"] = item_to_cat
    except Exception:
        pass

def suggest_category_by_similarity(item: str, existing_categories: list) -> tuple:
    item_lower = item.lower().strip()
    cat_lower_to_original = {c.lower(): c for c in existing_categories}
    keyword_map = {
        'food and grocery': ['apple', 'orange', 'banana', 'mango', 'grapes', 'bread', 'milk', 'egg', 'cheese', 'rice', 'pasta', 'vegetable', 'fruit', 'avocado', 'tomato', 'potato', 'onion', 'carrot', 'coffee', 'tea', 'snack', 'pizza', 'burger'],
        'fruits': ['apple', 'orange', 'banana', 'mango', 'grapes', 'watermelon', 'pineapple'],
        'commute': ['bus', 'train', 'taxi', 'uber', 'petrol', 'fuel', 'metro', 'auto'],
        'outing': ['movie', 'restaurant', 'cafe', 'dinner', 'lunch', 'bar'],
        'grocery': ['grocery', 'supermarket', 'store', 'produce'],
        'rent': ['rent', 'lease', 'apartment'],
        'bills': ['electricity', 'water', 'gas', 'bill', 'broadband', 'internet'],
        'automotive': ['tyre', 'tire', 'oil', 'service', 'repair'],
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
        best_original = cat_lower_to_original[matches[0]]
        return best_original, 0.7
    return None, 0.0

# ----------------------------------------------------------------------
# Intent classification: if pending state, force write
# ----------------------------------------------------------------------
def classify_intent(state: AgentState) -> AgentState:
    if state.get("pending_state"):
        state["intent"] = "write"
        return state

    text = state["user_query"].lower().strip()
    query_indicators = [
        "how much", "what is", "how many", "show me", "tell me", 
        "summarize", "total", "report", "list", "get", "view",
        "did i spend", "have i spent", "where did i", "when did i"
    ]
    if any(indicator in text for indicator in query_indicators):
        state["intent"] = "query"
        return state
    # Only 'add' and related words – removed 'update', 'change', 'set', etc.
    write_keywords = [
        "add", "create", "new", "bought", "purchase", "spent on", "paid for", 
        "transfer", "withdraw", "deposit"
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

# ----------------------------------------------------------------------
# Parse write: only supports add_expense
# ----------------------------------------------------------------------
def parse_write_intent(state: AgentState) -> AgentState:
    if state.get("pending_state"):
        state["parsed_write"] = state["pending_state"]["parsed"]
        return state

    if llm is None:
        state["parsed_write"] = {"operation": "unknown"}
        return state
    now = datetime.now()
    prompt = f"""Extract write operation details from user request. Return ONLY valid JSON.
Required fields: operation (must be "add_expense"), item (string), amount (number), day (int), month (int), year (int), notes (string).
If a field is missing, use defaults: day={now.day}, month={now.month}, year={now.year}, notes="" if not provided.
Today: day={now.day}, month={now.month}, year={now.year}.
User request: {state["user_query"]}
Example: {{"operation":"add_expense","item":"Coffee","amount":250,"day":15,"month":5,"year":2026,"notes":"morning coffee"}}
Now output JSON:"""
    response = llm.invoke(prompt).content.strip()
    match = re.search(r'\{.*\}', response, re.DOTALL)
    if not match:
        amount_match = re.search(r'\b(\d+(?:\.\d+)?)\b', state["user_query"])
        amount = float(amount_match.group(1)) if amount_match else 0
        words = state["user_query"].split()
        item = "Unknown"
        for w in words:
            if w.lower() not in ["add", "for", "rs", "rupees", "₹", "on", "at"] and not w.replace('.','').isdigit():
                item = w.title()
                break
        parsed = {"operation":"add_expense","item":item,"amount":amount,"day":now.day,"month":now.month,"year":now.year,"notes":""}
        state["parsed_write"] = parsed
        return state
    json_str = match.group(0)
    try:
        parsed = json.loads(json_str)
        if "operation" not in parsed:
            parsed["operation"] = "add_expense"
        if "amount" not in parsed or parsed["amount"] == 0:
            amount_match = re.search(r'\b(\d+(?:\.\d+)?)\b', state["user_query"])
            if amount_match:
                parsed["amount"] = float(amount_match.group(1))
            else:
                parsed["amount"] = 0
        if "item" not in parsed or not parsed["item"]:
            words = state["user_query"].split()
            for w in words:
                if w.lower() not in ["add", "for", "rs", "rupees", "₹", "on", "at"] and not w.replace('.','').isdigit():
                    parsed["item"] = w.title()
                    break
            if "item" not in parsed:
                parsed["item"] = "Unknown"
        parsed["day"] = parsed.get("day", now.day)
        parsed["month"] = parsed.get("month", now.month)
        parsed["year"] = parsed.get("year", now.year)
        parsed["notes"] = parsed.get("notes", "")
    except:
        parsed = {"operation":"unknown","error":"parse_failed"}
    state["parsed_write"] = parsed
    return state

def execute_write(state: AgentState) -> AgentState:
    pending = state.get("pending_state")
    if pending:
        step = pending.get("step")
        user_answer = state["user_query"].strip()
        state["parsed_write"] = pending.get("parsed", {})
        state["data_context"] = pending.get("data_context", {})
        state["pending_question"] = None
        state["pending_state"] = None
    else:
        step = None
        user_answer = None

    _refresh_data_context(state)
    parsed = state["parsed_write"]
    ctx = state["data_context"]
    writer = ctx.get("writer")
    item_to_cat = ctx.get("item_to_cat", {})
    existing_categories = sorted(set(str(v) for v in item_to_cat.values())) if item_to_cat else []

    if not writer:
        state["final_answer"] = "Writer not initialised."
        return state

    op = parsed.get("operation")
    if op != "add_expense":
        state["final_answer"] = "Only 'add' is supported. Please say 'add [item] [amount]'."
        return state

    # Get base values from parsed or pending
    if step is not None:
        item = pending.get("item", parsed.get("item", ""))
        amount = pending.get("amount", float(parsed.get("amount", 0)))
        day = pending.get("day", int(parsed.get("day", datetime.now().day)))
        month = pending.get("month", int(parsed.get("month", datetime.now().month)))
        year = pending.get("year", int(parsed.get("year", datetime.now().year)))
        user_notes = pending.get("notes", parsed.get("notes", ""))
    else:
        item = parsed.get("item", "")
        amount = float(parsed.get("amount", 0))
        day = int(parsed.get("day", datetime.now().day))
        month = int(parsed.get("month", datetime.now().month))
        year = int(parsed.get("year", datetime.now().year))
        user_notes = parsed.get("notes", "")

    if not item:
        state["final_answer"] = "Could not identify the item name."
        return state
    item = item.title()
    if not user_notes:
        user_notes = f"Added on {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    def ci_in_dict(key, d):
        return any(k.lower() == key.lower() for k in d.keys())
    def ci_get_dict(key, d):
        for k, v in d.items():
            if k.lower() == key.lower():
                return v
        return None

    # --- Step 1: Ask for amount if missing ---
    if step is None and amount == 0:
        state["pending_question"] = f"💰 How much did you spend on {item}? (e.g., 150)"
        state["pending_state"] = {
            "step": "amount",
            "parsed": parsed,
            "data_context": ctx,
            "item": item,
            "day": day,
            "month": month,
            "year": year,
            "notes": user_notes
        }
        return state

    # --- Step 2: Process amount reply and ask category immediately ---
    if step == "amount":
        amount_match = re.search(r'\b(\d+(?:\.\d+)?)\b', user_answer)
        if not amount_match:
            state["final_answer"] = "❌ Invalid amount. Please send a number, e.g., '150'."
            return state
        amount = float(amount_match.group(1))
        # Now go to category suggestion (no intermediate reply)
        suggested_cat, conf = suggest_category_by_similarity(item, existing_categories)
        if suggested_cat:
            state["pending_question"] = f"Match '{item}' to '{suggested_cat}'? (y/n) or type a new category name"
            state["pending_state"] = {
                "step": "category_confirm",
                "parsed": parsed,
                "data_context": ctx,
                "item": item,
                "amount": amount,
                "day": day,
                "month": month,
                "year": year,
                "notes": user_notes,
                "suggested": suggested_cat
            }
        else:
            state["pending_question"] = f"Create new category for '{item}':"
            state["pending_state"] = {
                "step": "newcat",
                "parsed": parsed,
                "data_context": ctx,
                "item": item,
                "amount": amount,
                "day": day,
                "month": month,
                "year": year,
                "notes": user_notes
            }
        return state

    # --- Step 3: Category confirmation (with suggestion) ---
    if step == "category_confirm":
        cat_response = user_answer.lower()
        suggested = pending.get("suggested")
        if cat_response.startswith('y'):
            category = suggested
            writer.add_category_mapping(item, category)
            _refresh_data_context(state)
        elif cat_response.startswith('n'):
            # User rejected suggestion – ask for new category
            state["pending_question"] = f"Create new category for '{item}':"
            state["pending_state"] = {
                "step": "newcat",
                "parsed": parsed,
                "data_context": ctx,
                "item": item,
                "amount": amount,
                "day": day,
                "month": month,
                "year": year,
                "notes": user_notes
            }
            return state
        else:
            # User typed a new category name
            category = cat_response.title()
            writer.add_category_mapping(item, category)
            _refresh_data_context(state)
        # Proceed to add expense
        result = writer.add_expense(year, item, amount, day, month, notes=user_notes)
        _refresh_data_context(state)
        date_str = f"{day}/{month}/{year}"
        success_msg = (
            f"✅ Added expense:\n"
            f"📦 Item: {item}\n"
            f"💰 Amount: ₹{amount:.2f}\n"
            f"📂 Category: {category}\n"
            f"📝 Note: {user_notes}\n"
            f"📅 Date: {date_str}"
        )
        state["final_answer"] = success_msg
        state["pending_question"] = None
        state["pending_state"] = None
        return state

    # --- Step 4: New category creation (no suggestion or after rejection) ---
    if step == "newcat":
        category = user_answer.title()
        if not category:
            category = "Unexpected"
        writer.add_category_mapping(item, category)
        _refresh_data_context(state)
        result = writer.add_expense(year, item, amount, day, month, notes=user_notes)
        _refresh_data_context(state)
        date_str = f"{day}/{month}/{year}"
        success_msg = (
            f"✅ Added expense:\n"
            f"📦 Item: {item}\n"
            f"💰 Amount: ₹{amount:.2f}\n"
            f"📂 Category: {category}\n"
            f"📝 Note: {user_notes}\n"
            f"📅 Date: {date_str}"
        )
        state["final_answer"] = success_msg
        state["pending_question"] = None
        state["pending_state"] = None
        return state

    # --- If we are starting fresh (amount already provided) ---
    if step is None and amount > 0:
        # Already have amount, go to category suggestion
        suggested_cat, conf = suggest_category_by_similarity(item, existing_categories)
        if suggested_cat:
            state["pending_question"] = f"Match '{item}' to '{suggested_cat}'? (y/n) or type a new category name"
            state["pending_state"] = {
                "step": "category_confirm",
                "parsed": parsed,
                "data_context": ctx,
                "item": item,
                "amount": amount,
                "day": day,
                "month": month,
                "year": year,
                "notes": user_notes,
                "suggested": suggested_cat
            }
        else:
            state["pending_question"] = f"Create new category for '{item}':"
            state["pending_state"] = {
                "step": "newcat",
                "parsed": parsed,
                "data_context": ctx,
                "item": item,
                "amount": amount,
                "day": day,
                "month": month,
                "year": year,
                "notes": user_notes
            }
        return state

    state["final_answer"] = "Unexpected state. Please start over."
    return state

def answer_query_node(state: AgentState) -> AgentState:
    _refresh_data_context(state)
    if llm is None:
        state["final_answer"] = "LLM not available."
        return state
    user_q = state["user_query"].lower().strip()
    if user_q in ["hi", "hello", "hey", "good morning", "good evening", "greetings", "how are you"]:
        state["final_answer"] = "Hello! I'm your finance assistant. You can add expenses or ask about your spending. For example: 'Add coffee 250 rs today' or 'How much did I spend on food?'."
        return state
    actual = state["data_context"]["actual"]
    budget = state["data_context"]["budget"]
    total = state["data_context"]["total_actual"]
    lines = [f"Total expenses: ₹{total:.2f}"]
    lines.append("\nCategory breakdown (Actual vs Budget):")
    for cat in sorted(actual.keys()):
        act = actual.get(cat, 0)
        bud = budget.get(cat, 0)
        diff = act - bud
        status = "over" if diff > 0 else "under" if diff < 0 else "on track"
        lines.append(f"  {cat}: ₹{act:.2f} vs ₹{bud:.2f} ({status} by ₹{abs(diff):.2f})")
    data_text = "\n".join(lines)
    prompt = f"Answer this finance question using only the data provided. If the question is not about finances, ignore the data and respond helpfully.\nData:\n{data_text}\nQuestion: {state['user_query']}\nAnswer:"
    state["final_answer"] = llm.invoke(prompt).content
    return state

def route_intent(state: AgentState) -> Literal["execute_write", "answer_query"]:
    return "execute_write" if state["intent"] == "write" else "answer_query"

def build_graph(writer: DataWriter, sheet_client: SheetClient, data_context: Dict):
    data_context["writer"] = writer
    data_context["sheet_client"] = sheet_client
    workflow = StateGraph(AgentState)
    workflow.add_node("classify_intent", classify_intent)
    workflow.add_node("parse_write", parse_write_intent)
    workflow.add_node("execute_write", execute_write)
    workflow.add_node("answer_query", answer_query_node)
    workflow.set_entry_point("classify_intent")
    workflow.add_edge("classify_intent", "parse_write")
    workflow.add_conditional_edges("parse_write", route_intent, {"execute_write": "execute_write", "answer_query": "answer_query"})
    workflow.add_edge("execute_write", END)
    workflow.add_edge("answer_query", END)
    compiled = workflow.compile()

    def run(state: AgentState):
        state["data_context"] = data_context
        return compiled.invoke(state)
    return run