# tests/test_data_loader.py
"""
Offline regression tests for data_loader.py's sheet-parsing edge cases.
No real SheetClient, no network -- a minimal fake with just get_all_values()
is enough since these functions never write anything.
"""

import logging
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_loader import load_expense_journal, load_budget, get_actual_spending


class FakeValuesClient:
    def __init__(self, data):
        self.data = data

    def get_all_values(self, name):
        return self.data.get(name, [])

    def get_worksheet(self, name):
        raise NotImplementedError("not needed for these tests")


class TestItemWhitespaceStrip(unittest.TestCase):
    def test_whitespace_variant_items_collapse_into_one_category_total(self):
        """FIX (issue #33): "Coffee ", " coffee", "COFFEE" must all resolve to
        the same category and sum together, not silently lose spend."""
        client = FakeValuesClient({
            "Expense Journal": [
                ["Year", "Item", "Amount (₹)", "Day", "Month", "F", "G", "Notes"],
                ["2026", "Coffee ", "100", "1", "8", "", "", ""],
                ["2026", " coffee", "50", "1", "8", "", "", ""],
                ["2026", "COFFEE", "25", "1", "8", "", "", ""],
            ]
        })
        df = load_expense_journal(client)
        self.assertEqual(sorted(df["Item"].str.lower().unique()), ["coffee"])

        actual, total = get_actual_spending(df, {"coffee": "Food"})
        self.assertEqual(actual, {"Food": 175.0})
        self.assertEqual(total, 175.0)


class TestTotalFilterCaseInsensitive(unittest.TestCase):
    def test_total_variants_are_all_filtered(self):
        """FIX (issue #35): "TOTAL"/"total" (not just exactly "Total") must
        not become a phantom budget category."""
        client = FakeValuesClient({
            "Category Budget": [
                ["Category", "Amount"],
                ["Groceries", "5000"],
                ["Total", "999999"],
                ["TOTAL", "888888"],
                ["total", "777777"],
            ]
        })
        budget = load_budget(client, "Category Budget")
        self.assertEqual(budget, {"Groceries": 5000.0})


class TestMalformedAmountLogged(unittest.TestCase):
    def test_unparseable_amount_is_dropped_and_logged(self):
        """FIX (issue #34): a malformed Amount cell must be dropped (not
        crash, not silently coerce to something wrong) AND logged with the
        raw value, so it's at least visible to someone checking logs."""
        client = FakeValuesClient({
            "Expense Journal": [
                ["Year", "Item", "Amount (₹)", "Day", "Month", "F", "G", "Notes"],
                ["2026", "BadRow", "#REF!", "1", "8", "", "", ""],
                ["2026", "GoodRow", "100", "1", "8", "", "", ""],
            ]
        })
        logs = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                logs.append(record.getMessage())

        logger = logging.getLogger("data_loader")
        handler = CaptureHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            df = load_expense_journal(client)
        finally:
            logger.removeHandler(handler)

        self.assertEqual(len(df), 1, "the malformed row must be dropped, the good one kept")
        self.assertEqual(df.iloc[0]["Item"], "GoodRow")
        self.assertTrue(
            any("BadRow" in m and "#REF!" in m for m in logs),
            f"expected a warning naming the dropped row and its raw value, got: {logs}",
        )


if __name__ == "__main__":
    unittest.main()
