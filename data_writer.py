# data_writer.py

from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class DataWriter:
    def __init__(self, sheet_client):
        self.sheet_client = sheet_client

    def add_expense(self, year, item, amount, day, month, notes=""):
        """
        Write a new expense row to columns A–E and H atomically.
        Columns F and G are never written — reserved for sheet formulas.

        FIX (issue #6): the original code made 6 separate update_cell() calls.
        A network failure after the first call left a half-written, corrupt row
        with no way to detect or roll back.  Replaced with a single
        sheet.update(range, values) call so all 5 data columns are written in
        one API request.  Column H (notes) is written separately because it is
        not contiguous with A–E, but the expense data itself is atomic.
        """
        item = item.title()
        if not notes or len(notes.strip()) < 2:
            now = datetime.now()
            notes = f"Added via bot on {now.strftime('%Y-%m-%d %H:%M:%S')}"

        sheet   = self.sheet_client.get_worksheet("Expense Journal")
        col_a   = sheet.col_values(1)
        next_row = max(len(col_a) + 1, 2)

        try:
            # Write A:E in a single API call (atomic for the core expense data)
            range_notation = f"A{next_row}:E{next_row}"
            sheet.update(
                range_notation,
                [[str(year), item, float(amount), str(day), str(month)]],
                value_input_option='USER_ENTERED',
            )
            # Write H (notes) separately — not contiguous with A–E
            sheet.update_cell(next_row, 8, notes)
            return (
                f"✅ Added expense: {item} for ₹{amount:.2f} "
                f"on {day}/{month}/{year}. Note: {notes}"
            )
        except Exception as e:
            logger.exception("add_expense failed at row %d", next_row)
            return f"❌ Failed to add expense: {str(e)}"

    def add_category_mapping(self, item_name, category):
        """
        Add a new row to the Item & Category sheet.
        Both values stored in Title Case.
        """
        item_name = item_name.title()
        category  = category.title()
        try:
            success = self.sheet_client.append_row(
                "Item & Category", [item_name, category]
            )
            if success:
                return f"✅ Added new mapping: '{item_name}' → '{category}'."
            return "❌ Failed to add category mapping (sheet write returned False)."
        except Exception as e:
            logger.exception("add_category_mapping failed")
            return f"❌ Failed to add category mapping: {str(e)}"
