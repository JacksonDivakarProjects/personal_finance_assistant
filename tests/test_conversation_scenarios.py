# tests/test_conversation_scenarios.py
"""
Offline, deterministic regression suite for agent.py's conversation state
machine, focused on the LLM-combined-classify-and-extract refactor.

HARD CONSTRAINTS enforced throughout:
  - No real SheetClient is ever constructed. FakeSheetClient (tests/fakes.py)
    is an in-memory stand-in with the same method surface
    (get_worksheet/append_row/update_cell/get_all_values).
  - No real Groq call is ever made. agent.llm is monkeypatched to a
    ScriptedLLM whose .invoke(prompt) returns pre-scripted content and raises
    loudly (AssertionError) if called more times than scripted -- so an
    un-mocked network call would fail the test instead of silently degrading
    it or hitting the network.
  - test_write.py is never imported, run, or modified.
  - No new third-party dependency is used; only unittest/unittest.mock (stdlib).

Run with:
    .venv/Scripts/python.exe -m unittest tests.test_conversation_scenarios -v
"""

import sys
import os
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
from data_writer import DataWriter
from tests.fakes import make_fake_sheet_client, ScriptedLLM


# ── Test scaffolding ──────────────────────────────────────────────────────────

class SpyWriter(DataWriter):
    """
    Real DataWriter wired to a FakeSheetClient, plus a call log so we can
    assert exact args passed to add_expense/add_category_mapping (item order,
    types, call counts) -- not just that "some write happened".
    """

    def __init__(self, sheet_client):
        super().__init__(sheet_client)
        self.add_expense_calls = []
        self.add_category_mapping_calls = []

    def add_expense(self, year, item, amount, day, month, notes=""):
        self.add_expense_calls.append(
            dict(year=year, item=item, amount=amount, day=day, month=month, notes=notes)
        )
        return super().add_expense(year, item, amount, day, month, notes=notes)

    def add_category_mapping(self, item_name, category):
        self.add_category_mapping_calls.append(dict(item_name=item_name, category=category))
        return super().add_category_mapping(item_name, category)


def make_state(user_query, pending_state=None):
    """Always construct all seven AgentState keys -- an incomplete AgentState
    is itself the kind of bug this suite exists to catch (see bot.py's own
    comment re: issue #17)."""
    return {
        "user_query": user_query,
        "intent": None,
        "parsed_write": None,
        "data_context": {},
        "final_answer": "",
        "pending_question": None,
        "pending_state": pending_state,
    }


def build_env(item_category_rows=None):
    sheet_client = make_fake_sheet_client(item_category_rows)
    writer = SpyWriter(sheet_client)
    data_context = {}
    run_agent = agent.build_graph(writer, sheet_client, data_context)
    return sheet_client, writer, data_context, run_agent


CLASSIFY_MARKER = "intent classifier"        # substring unique to the combined classify prompt
PARSE_WRITE_MARKER = "Extract write operation details"  # substring unique to parse_write_intent's own prompt


# ── 1. Greeting short-circuit ─────────────────────────────────────────────────

class TestGreetingShortCircuit(unittest.TestCase):
    def test_hi_skips_llm_entirely(self):
        _, writer, _, run_agent = build_env()
        scripted = ScriptedLLM([])  # zero scripted responses: any invoke() call fails the test
        with patch.object(agent, "llm", scripted):
            result = run_agent(make_state("hi"))

        self.assertEqual(result["intent"], "query")
        self.assertEqual(len(scripted.calls), 0, "greeting must not call the LLM at all")
        self.assertIn("finance assistant", result["final_answer"])
        self.assertEqual(len(writer.add_expense_calls), 0)


# ── 2. Pending-state regression: amount step + category_confirm step ─────────

class TestPendingStateRoundTrip(unittest.TestCase):
    def test_amount_then_category_confirm_end_to_end(self):
        _, writer, _, run_agent = build_env()  # empty Item & Category sheet

        # Turn 1: "add mango" -- item known, amount missing.
        turn1_llm_json = (
            '{"intent":"write","operation":"add_expense","item":"Mango","amount":0,'
            '"day":10,"month":7,"year":2026,"notes":""}'
        )
        scripted = ScriptedLLM([turn1_llm_json])
        with patch.object(agent, "llm", scripted):
            r1 = run_agent(make_state("add mango"))

        self.assertEqual(len(scripted.calls), 1, "classify+extract should be ONE combined call")
        self.assertIsNone(r1["final_answer"] or None)  # no final answer yet
        self.assertIn("How much did you spend on Mango", r1["pending_question"])
        pending1 = r1["pending_state"]
        self.assertEqual(pending1["step"], "amount")
        self.assertEqual(pending1["item"], "Mango")
        self.assertEqual(pending1["day"], 10)
        self.assertEqual(pending1["month"], 7)
        self.assertEqual(pending1["year"], 2026)

        # Turn 2: user replies "150" -- amount step completes, item unknown ->
        # category suggestion offered. NO llm call should happen (pending_state
        # branch is unrelated to the refactor and must not regress).
        scripted2 = ScriptedLLM([])
        with patch.object(agent, "llm", scripted2):
            r2 = run_agent(make_state("150", pending_state=pending1))

        self.assertEqual(len(scripted2.calls), 0, "pending amount-step turn must not call the LLM")
        self.assertIn("Match 'Mango' to category", r2["pending_question"])
        pending2 = r2["pending_state"]
        self.assertEqual(pending2["step"], "category_confirm")
        self.assertEqual(pending2["item"], "Mango")
        self.assertEqual(pending2["amount"], 150.0)
        self.assertEqual(pending2["day"], 10)
        self.assertEqual(pending2["month"], 7)
        self.assertEqual(pending2["year"], 2026)
        self.assertEqual(pending2["suggested"], "Food And Grocery")
        self.assertEqual(len(writer.add_expense_calls), 0, "must not write before category is resolved")

        # Turn 3: user replies "y" -- category_confirm completes, single write.
        scripted3 = ScriptedLLM([])
        with patch.object(agent, "llm", scripted3):
            r3 = run_agent(make_state("y", pending_state=pending2))

        self.assertEqual(len(scripted3.calls), 0, "pending category_confirm-step turn must not call the LLM")
        self.assertIsNone(r3["pending_question"])
        self.assertIsNone(r3["pending_state"])
        self.assertEqual(len(writer.add_expense_calls), 1, "exactly one write, no double-write")
        call = writer.add_expense_calls[0]
        self.assertEqual(call["item"], "Mango")
        self.assertEqual(call["amount"], 150.0)
        self.assertEqual(call["day"], 10)
        self.assertEqual(call["month"], 7)
        self.assertEqual(call["year"], 2026)
        self.assertEqual(len(writer.add_category_mapping_calls), 1)
        self.assertEqual(writer.add_category_mapping_calls[0],
                          {"item_name": "Mango", "category": "Food And Grocery"})
        self.assertIn("Food And Grocery", r3["final_answer"])


# ── 3. Combined call, single round-trip, write intent ────────────────────────

class TestCombinedCallWriteIntent(unittest.TestCase):
    def test_single_message_single_llm_call_completes_write(self):
        now = datetime.now()
        # Item already mapped -> single-turn completion (no follow-up question).
        _, writer, _, run_agent = build_env(
            item_category_rows=[["Item name", "Category"], ["Pizza", "Food And Grocery"]]
        )
        llm_json = (
            '{"intent":"write","operation":"add_expense","item":"Pizza","amount":300,'
            f'"day":{now.day},"month":{now.month},"year":{now.year},"notes":""}}'
        )
        scripted = ScriptedLLM([llm_json])
        with patch.object(agent, "llm", scripted):
            result = run_agent(make_state("spent 300 on pizza"))

        self.assertEqual(len(scripted.calls), 1,
                          "classify+extract phase must be exactly ONE llm.invoke call")
        self.assertEqual(result["intent"], "write")
        self.assertIsNone(result["pending_question"])
        self.assertIsNone(result["pending_state"])
        self.assertEqual(len(writer.add_expense_calls), 1)
        call = writer.add_expense_calls[0]
        self.assertEqual(call["item"], "Pizza")
        self.assertEqual(call["amount"], 300.0)
        self.assertEqual(call["day"], now.day)
        self.assertEqual(call["month"], now.month)
        self.assertEqual(call["year"], now.year)
        self.assertEqual(len(writer.add_category_mapping_calls), 0,
                          "item already mapped -- must reuse, not create a new mapping")
        self.assertIn("Pizza", result["final_answer"])
        self.assertIn("300.00", result["final_answer"])


# ── 4. Combined call, ambiguous query phrasing ────────────────────────────────

class TestCombinedCallQueryIntent(unittest.TestCase):
    def test_ambiguous_query_routes_to_answer_query_without_parse_write(self):
        _, writer, _, run_agent = build_env()
        answer_text = "Rent is one of your biggest categories this month."
        scripted = ScriptedLLM(['{"intent":"query"}', answer_text])
        with patch.object(agent, "llm", scripted):
            result = run_agent(make_state("rent bill is getting expensive these days"))

        self.assertEqual(result["intent"], "query")
        classify_calls = [c for c in scripted.calls if CLASSIFY_MARKER in c]
        parse_write_calls = [c for c in scripted.calls if PARSE_WRITE_MARKER in c]
        self.assertEqual(len(classify_calls), 1, "classification must happen exactly once")
        self.assertEqual(len(parse_write_calls), 0,
                          "parse_write's own LLM call must never fire for a query-routed message")
        self.assertEqual(len(scripted.calls), 2,
                          "total calls = 1 classify + 1 answer_query_node generation, no more")
        self.assertEqual(result["final_answer"], answer_text)
        self.assertEqual(len(writer.add_expense_calls), 0)


# ── 5/6/7: classify_intent fallback behaviour (isolated node call) ───────────

class TestClassifyFallbackBehaviour(unittest.TestCase):
    def test_exception_on_classify_call_falls_back_to_rule_based(self):
        cases = [
            ("add coffee 250", "write"),
            ("how much did I spend on food", "query"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                scripted = ScriptedLLM([RuntimeError("groq is down")])
                with patch.object(agent, "llm", scripted):
                    state = make_state(text)
                    result = agent.classify_intent(state)
                expected_rule_based = agent._classify_intent_rule_based(make_state(text))["intent"]
                self.assertEqual(expected_rule_based, expected)  # sanity: matches old behaviour
                self.assertEqual(result["intent"], expected)
                self.assertEqual(len(scripted.calls), 1)

    def test_garbage_json_on_classify_call_falls_back_to_rule_based(self):
        cases = [
            ("add coffee 250", "write"),
            ("how much did I spend on food", "query"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                scripted = ScriptedLLM(["this is not json at all, sorry"])
                with patch.object(agent, "llm", scripted):
                    state = make_state(text)
                    result = agent.classify_intent(state)
                self.assertEqual(result["intent"], expected)
                self.assertEqual(len(scripted.calls), 1)

    def test_write_intent_missing_item_returns_none_from_extractor(self):
        scripted = ScriptedLLM(['{"intent":"write","operation":"add_expense","amount":250}'])
        with patch.object(agent, "llm", scripted):
            result = agent._classify_and_extract_via_llm("some ambiguous message")
        self.assertIsNone(result, "no item name -> must return None, not a garbage write")

        # And end-to-end: classify_intent must fall back to rule-based rather
        # than crash or silently write a garbage item.
        scripted2 = ScriptedLLM(['{"intent":"write","operation":"add_expense","amount":250}'])
        with patch.object(agent, "llm", scripted2):
            state = make_state("add something 250")
            result_state = agent.classify_intent(state)
        self.assertEqual(len(scripted2.calls), 1)
        # rule-based: "add" keyword -> write
        self.assertEqual(result_state["intent"], "write")
        # Crucially: parsed_write was NOT stashed from the failed LLM extraction.
        self.assertIsNone(result_state.get("parsed_write"))


# ── 8. Full regression pass ───────────────────────────────────────────────────

class TestFullRegressionPass(unittest.TestCase):
    def _write_via_llm(self, run_agent, message, item, amount, day=10, month=7, year=2026, notes=""):
        llm_json = (
            f'{{"intent":"write","operation":"add_expense","item":"{item}","amount":{amount},'
            f'"day":{day},"month":{month},"year":{year},"notes":"{notes}"}}'
        )
        scripted = ScriptedLLM([llm_json])
        with patch.object(agent, "llm", scripted):
            return run_agent(make_state(message)), scripted

    def test_category_confirm_yeah_affirmative(self):
        _, writer, _, run_agent = build_env()
        r1, _ = self._write_via_llm(run_agent, "add coffee 100", "Coffee", 100)
        pending = r1["pending_state"]
        self.assertEqual(pending["step"], "category_confirm")
        scripted2 = ScriptedLLM([])
        with patch.object(agent, "llm", scripted2):
            r2 = run_agent(make_state("yeah", pending_state=pending))
        self.assertEqual(writer.add_category_mapping_calls[-1]["category"], "Food And Grocery")
        self.assertEqual(len(writer.add_expense_calls), 1)
        self.assertIsNone(r2["pending_state"])

    def test_category_confirm_yes_with_punctuation(self):
        _, writer, _, run_agent = build_env()
        r1, _ = self._write_via_llm(run_agent, "add coffee 100", "Coffee", 100)
        pending = r1["pending_state"]
        with patch.object(agent, "llm", ScriptedLLM([])):
            r2 = run_agent(make_state("Yes!", pending_state=pending))
        self.assertEqual(writer.add_category_mapping_calls[-1]["category"], "Food And Grocery")
        self.assertIsNone(r2["pending_state"])

    def test_category_confirm_literal_yoga_not_treated_as_yes(self):
        _, writer, _, run_agent = build_env()
        # "movie" -> suggested category "Outing" (keyword match)
        r1, _ = self._write_via_llm(run_agent, "add movie 250", "Movie", 250)
        pending = r1["pending_state"]
        self.assertEqual(pending["suggested"], "Outing")
        with patch.object(agent, "llm", ScriptedLLM([])):
            r2 = run_agent(make_state("Yoga", pending_state=pending))
        self.assertEqual(writer.add_category_mapping_calls[-1],
                          {"item_name": "Movie", "category": "Yoga"})
        self.assertNotEqual(writer.add_category_mapping_calls[-1]["category"], "Outing")

    def test_category_confirm_literal_news_not_treated_as_no(self):
        _, writer, _, run_agent = build_env()
        r1, _ = self._write_via_llm(run_agent, "add movie 250", "Movie", 250)
        pending = r1["pending_state"]
        with patch.object(agent, "llm", ScriptedLLM([])):
            r2 = run_agent(make_state("News", pending_state=pending))
        self.assertEqual(writer.add_category_mapping_calls[-1],
                          {"item_name": "Movie", "category": "News"})
        # And confirm it did NOT take the "no" branch (which would re-prompt "newcat").
        self.assertIsNone(r2["pending_state"])
        self.assertEqual(len(writer.add_expense_calls), 1)

    def test_duplicate_item_case_insensitive_reuses_mapping(self):
        _, writer, _, run_agent = build_env(
            item_category_rows=[["Item name", "Category"], ["Coffee", "Food And Grocery"]]
        )
        result, scripted = self._write_via_llm(run_agent, "add coffee 50", "coffee", 50)
        self.assertEqual(len(scripted.calls), 1)
        self.assertIsNone(result["pending_state"], "already mapped -> single-turn write, no prompt")
        self.assertEqual(len(writer.add_category_mapping_calls), 0,
                          "must reuse existing 'Coffee' mapping, not create a second one")
        self.assertEqual(len(writer.add_expense_calls), 1)
        self.assertEqual(writer.add_expense_calls[0]["item"], "Coffee")  # title-cased on write

    def test_malformed_amount_reply(self):
        """
        Item name known, amount missing -> pending 'amount' step. User replies
        with non-numeric text. FIX (issue #26): re-prompt for the amount
        without losing item/day/month/year, instead of dropping the whole
        in-progress entry.
        """
        _, writer, _, run_agent = build_env()
        r1, _ = self._write_via_llm(run_agent, "add mango 0", "Mango", 0, day=5, month=6, year=2026)
        pending = r1["pending_state"]
        self.assertEqual(pending["step"], "amount")

        with patch.object(agent, "llm", ScriptedLLM([])):
            r2 = run_agent(make_state("abc", pending_state=pending))

        self.assertEqual(len(writer.add_expense_calls), 0, "must not silently write amount=0")
        self.assertIsNotNone(r2["pending_question"], "must re-ask for the amount, not dead-end")
        self.assertIsNotNone(
            r2["pending_state"],
            "issue #26 fix: a malformed amount reply must preserve item/day/month/year, not drop them",
        )
        self.assertEqual(r2["pending_state"]["step"], "amount")
        self.assertEqual(r2["pending_state"]["item"], "Mango")
        self.assertEqual(r2["pending_state"]["day"], 5)
        self.assertEqual(r2["pending_state"]["month"], 6)
        self.assertEqual(r2["pending_state"]["year"], 2026)

    def test_interrupted_flow_query_during_pending_write(self):
        """
        User is mid-write (pending amount step) and sends an unrelated
        query-looking message. classify_intent forces intent='write' whenever
        pending_state is set, so the interrupting message is fed straight into
        execute_write's amount step, not answered as a query. Since it has no
        digits, it's treated as an invalid amount reply -- FIX (issue #26)
        means the original in-progress entry now survives that instead of
        being dropped.
        """
        _, writer, _, run_agent = build_env()
        r1, _ = self._write_via_llm(run_agent, "add mango 0", "Mango", 0, day=5, month=6, year=2026)
        pending = r1["pending_state"]
        self.assertEqual(pending["step"], "amount")

        interrupting_message = "how much did I spend on food this month"
        with patch.object(agent, "llm", ScriptedLLM([])):
            r2 = run_agent(make_state(interrupting_message, pending_state=pending))

        self.assertEqual(r2.get("intent"), "write")
        self.assertEqual(len(writer.add_expense_calls), 0)
        self.assertIsNotNone(
            r2["pending_state"],
            "issue #26 fix: original in-progress entry (item=Mango, day=5/month=6/year=2026) "
            "must survive an interrupting message misrouted into the amount step",
        )
        self.assertEqual(r2["pending_state"]["item"], "Mango")
        self.assertEqual(r2["pending_state"]["day"], 5)
        self.assertEqual(r2["pending_state"]["month"], 6)
        self.assertEqual(r2["pending_state"]["year"], 2026)


# ── 9. Write-failure retry (issue #28) ────────────────────────────────────────

class FlakyWorksheetOnce:
    """Wraps a real FakeWorksheet so its first `update()` call raises, then
    behaves normally -- simulates a transient Sheets API failure (rate limit,
    network blip) on the first attempt only."""

    def __init__(self, real_worksheet):
        self._real = real_worksheet
        self._update_calls = 0

    def col_values(self, col_idx):
        return self._real.col_values(col_idx)

    def get_all_values(self):
        return self._real.get_all_values()

    def update(self, range_notation, values, value_input_option=None):
        self._update_calls += 1
        if self._update_calls == 1:
            raise RuntimeError("simulated Sheets API rate limit")
        return self._real.update(range_notation, values, value_input_option=value_input_option)

    def update_cell(self, row, col, value):
        return self._real.update_cell(row, col, value)

    def get_all_records(self):
        return self._real.get_all_records()


class FlakyOnceSheetClient:
    """Wraps a real FakeSheetClient so the first write to Expense Journal fails."""

    def __init__(self, real_client):
        self._real = real_client
        self._flaky_ws = None

    def get_worksheet(self, name):
        real_ws = self._real.get_worksheet(name)
        if name == "Expense Journal":
            if self._flaky_ws is None:
                self._flaky_ws = FlakyWorksheetOnce(real_ws)
            return self._flaky_ws
        return real_ws

    def append_row(self, sheet_name, row_data):
        return self._real.append_row(sheet_name, row_data)

    def update_cell(self, sheet_name, row, col, value):
        return self._real.update_cell(sheet_name, row, col, value)

    def get_all_values(self, sheet_name):
        return self._real.get_all_values(sheet_name)


class TestWriteFailureRetry(unittest.TestCase):
    def test_add_expense_failure_is_reported_and_retry_succeeds(self):
        """
        FIX (issue #28): execute_write used to ignore DataWriter's return value
        and always show a success message, even when the underlying Sheets
        write failed. Verify a failure is now (a) reported truthfully, (b)
        preserves enough pending_state to retry, and (c) the retry actually
        re-attempts and succeeds -- not just that the first call "handles"
        the error somehow.
        """
        real_client, _, _, _ = build_env(
            item_category_rows=[["Item name", "Category"], ["Coffee", "Food And Grocery"]]
        )
        flaky_client = FlakyOnceSheetClient(real_client)
        writer = SpyWriter(flaky_client)
        run_agent = agent.build_graph(writer, flaky_client, {})

        scripted = ScriptedLLM([
            '{"intent":"write","operation":"add_expense","item":"Coffee","amount":50,'
            '"day":1,"month":8,"year":2026,"notes":""}'
        ])
        with patch.object(agent, "llm", scripted):
            r1 = run_agent(make_state("add coffee 50"))

        self.assertEqual(len(writer.add_expense_calls), 1, "first attempt must actually try to write")
        self.assertIn("❌", r1["final_answer"], "a genuine write failure must not look like success")
        self.assertIsNotNone(r1["pending_question"], "must tell the user to retry, not go silent")
        pending = r1["pending_state"]
        self.assertIsNotNone(pending, "must preserve enough state to retry the identical write")
        self.assertEqual(pending["step"], "retry_write")
        self.assertEqual(pending["item"], "Coffee")
        self.assertEqual(pending["amount"], 50.0)
        self.assertEqual(pending["category"], "Food And Grocery")

        scripted2 = ScriptedLLM([])
        with patch.object(agent, "llm", scripted2):
            r2 = run_agent(make_state("retry", pending_state=pending))

        self.assertEqual(len(scripted2.calls), 0, "retry must not call the LLM")
        self.assertEqual(len(writer.add_expense_calls), 2, "retry must re-attempt the write")
        self.assertIsNone(r2["pending_state"], "must clear pending_state once the retry succeeds")
        self.assertIn("Added expense", r2["final_answer"])


# ── 10. next_row must not collide with a row whose Year cell is blank (issue #29) ─

class TestNextRowDoesNotOverwrite(unittest.TestCase):
    def test_blank_year_cell_does_not_get_overwritten(self):
        """
        FIX (issue #29): next_row used to be computed from col_values(1)
        (Year) alone. A real row with a blank Year cell but real Item/Amount/
        Day/Month data was invisible to that check, so a new expense would
        silently land on top of it and destroy the existing row. Seed exactly
        that shape and confirm the new expense is appended after it instead.
        """
        sheet_client, writer, _, run_agent = build_env(
            item_category_rows=[["Item name", "Category"], ["Movie Ticket", "Outing"]]
        )
        sheet_client.store["Expense Journal"] = [
            ["Year", "Item", "Amount (₹)", "Day", "Month", "Col6", "Col7", "Notes"],
            ["2026", "Rent", "20000", "1", "7", "", "", "July rent"],
            ["", "SalaryBonus", "50000", "15", "7", "", "", "bonus"],  # blank Year, real data
        ]

        scripted = ScriptedLLM([
            '{"intent":"write","operation":"add_expense","item":"Movie Ticket","amount":500,'
            '"day":20,"month":8,"year":2026,"notes":""}'
        ])
        with patch.object(agent, "llm", scripted):
            run_agent(make_state("add movie ticket 500"))

        rows = sheet_client.store["Expense Journal"]
        self.assertEqual(len(rows), 4, "must APPEND a new row, not overwrite an existing one")
        self.assertEqual(rows[2][1], "SalaryBonus", "the blank-Year row must survive untouched")
        self.assertEqual(rows[2][2], "50000")
        self.assertEqual(rows[3][1], "Movie Ticket", "the new expense must land on a genuinely new row")


# ── 11. Comma-formatted amounts must not be truncated (issue #30) ────────────

class TestCommaFormattedAmount(unittest.TestCase):
    def test_amount_step_reply_with_thousands_comma(self):
        """
        FIX (issue #30): the amount-step regex used to match only the first
        digit group of a comma-separated number -- "1,500" silently became
        1.0, not 1500.0, with a confident (wrong) success message.
        """
        _, writer, _, run_agent = build_env(
            item_category_rows=[["Item name", "Category"], ["Rent", "Bills"]]
        )
        r1, _ = self._write_via_llm_helper(run_agent, "add rent 0", "Rent", 0)
        pending = r1["pending_state"]
        self.assertEqual(pending["step"], "amount")

        with patch.object(agent, "llm", ScriptedLLM([])):
            r2 = run_agent(make_state("1,500", pending_state=pending))

        self.assertEqual(len(writer.add_expense_calls), 1)
        self.assertEqual(writer.add_expense_calls[0]["amount"], 1500.0,
                          "must parse the full comma-formatted amount, not just '1'")

    def _write_via_llm_helper(self, run_agent, message, item, amount, day=None, month=None, year=None):
        now = datetime.now()
        day, month, year = day or now.day, month or now.month, year or now.year
        llm_json = (
            f'{{"intent":"write","operation":"add_expense","item":"{item}","amount":{amount},'
            f'"day":{day},"month":{month},"year":{year},"notes":""}}'
        )
        scripted = ScriptedLLM([llm_json])
        with patch.object(agent, "llm", scripted):
            result = run_agent(make_state(message))
        return result, scripted


# ── 12. Greeting-prefix must not swallow real expense messages (issue #31) ───

class TestGreetingPrefixDoesNotSwallowWrites(unittest.TestCase):
    def test_greeting_prefixed_expense_message_still_reaches_llm_and_writes(self):
        """
        FIX (issue #31): "Hi, bought coffee for 250" used to be forced to
        intent=query by a prefix match on "hi", silently never writing the
        expense. It must now reach the combined LLM classify+extract call and
        complete the write.
        """
        _, writer, _, run_agent = build_env(
            item_category_rows=[["Item name", "Category"], ["Coffee", "Food And Grocery"]]
        )
        scripted = ScriptedLLM([
            '{"intent":"write","operation":"add_expense","item":"Coffee","amount":250,'
            '"day":1,"month":8,"year":2026,"notes":""}'
        ])
        with patch.object(agent, "llm", scripted):
            result = run_agent(make_state("Hi, bought coffee for 250"))

        self.assertEqual(len(scripted.calls), 1, "must reach the combined classify+extract call")
        self.assertEqual(result.get("intent"), "write")
        self.assertEqual(len(writer.add_expense_calls), 1)
        self.assertEqual(writer.add_expense_calls[0]["amount"], 250.0)

    def test_bare_greeting_still_short_circuits_without_llm(self):
        _, writer, _, run_agent = build_env()
        scripted = ScriptedLLM([])
        with patch.object(agent, "llm", scripted):
            result = run_agent(make_state("Hi!"))
        self.assertEqual(result.get("intent"), "query")
        self.assertEqual(len(scripted.calls), 0, "a bare greeting must still skip the LLM entirely")


# ── 13. Cancel keyword + input validation at every step (issue #32) ──────────

class TestCancelAndValidation(unittest.TestCase):
    def test_cancel_at_amount_step(self):
        _, writer, _, run_agent = build_env()
        r1, _ = self._add_via_llm(run_agent, "add mango 0", "Mango", 0)
        pending = r1["pending_state"]
        self.assertEqual(pending["step"], "amount")

        with patch.object(agent, "llm", ScriptedLLM([])):
            r2 = run_agent(make_state("cancel", pending_state=pending))

        self.assertIn("Cancelled", r2["final_answer"])
        self.assertIsNone(r2["pending_state"])
        self.assertIsNone(r2["pending_question"])
        self.assertEqual(len(writer.add_expense_calls), 0)

    def test_cancel_at_category_confirm_step(self):
        _, writer, _, run_agent = build_env()  # empty Item & Category -> triggers suggestion path
        r1, _ = self._add_via_llm(run_agent, "add coffee 100", "Coffee", 100)
        pending = r1["pending_state"]
        self.assertEqual(pending["step"], "category_confirm")

        with patch.object(agent, "llm", ScriptedLLM([])):
            r2 = run_agent(make_state("never mind", pending_state=pending))

        self.assertIn("Cancelled", r2["final_answer"])
        self.assertIsNone(r2["pending_state"])
        self.assertEqual(len(writer.add_expense_calls), 0)
        self.assertEqual(len(writer.add_category_mapping_calls), 0)

    def test_empty_reply_at_category_confirm_reprompts_instead_of_blank_category(self):
        """FIX (issue #32): an empty/whitespace reply used to silently become
        a permanent BLANK category. Must re-ask instead."""
        _, writer, _, run_agent = build_env()
        r1, _ = self._add_via_llm(run_agent, "add coffee 100", "Coffee", 100)
        pending = r1["pending_state"]
        self.assertEqual(pending["step"], "category_confirm")

        with patch.object(agent, "llm", ScriptedLLM([])):
            r2 = run_agent(make_state("   ", pending_state=pending))

        self.assertEqual(len(writer.add_category_mapping_calls), 0, "must not write a blank category")
        self.assertEqual(len(writer.add_expense_calls), 0)
        self.assertIsNotNone(r2["pending_state"], "must re-prompt, preserving the pending entry")
        self.assertEqual(r2["pending_state"]["step"], "category_confirm")

    def test_formula_injection_category_is_escaped(self):
        """FIX (issue #32): a category typed as '=1+1' must not become a live
        Sheets formula -- it should be stored as literal text."""
        sheet_client, writer, _, run_agent = build_env()
        r1, _ = self._add_via_llm(run_agent, "add coffee 100", "Coffee", 100)
        pending = r1["pending_state"]
        self.assertEqual(pending["step"], "category_confirm")

        with patch.object(agent, "llm", ScriptedLLM([])):
            run_agent(make_state("=1+1", pending_state=pending))

        mapping_rows = sheet_client.store["Item & Category"]
        self.assertEqual(mapping_rows[-1][1][0], "'", "formula-leading category must be stored as literal text")

    def _add_via_llm(self, run_agent, message, item, amount):
        now = datetime.now()
        llm_json = (
            f'{{"intent":"write","operation":"add_expense","item":"{item}","amount":{amount},'
            f'"day":{now.day},"month":{now.month},"year":{now.year},"notes":""}}'
        )
        scripted = ScriptedLLM([llm_json])
        with patch.object(agent, "llm", scripted):
            result = run_agent(make_state(message))
        return result, scripted


if __name__ == "__main__":
    unittest.main()
