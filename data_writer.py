# data_writer.py (no update features – only add expense and category mapping)

from sheet_client import SheetClient
from datetime import datetime

class DataWriter:
    def __init__(self, sheet_client):
        self.sheet_client = sheet_client

    def add_expense(self, year, item, amount, day, month, notes=""):
        """
        Write a new expense row ONLY to columns A(Year), B(Item), C(Amount), D(Day), E(Month), H(Notes).
        Item is stored in Title Case.
        Columns F and G are NEVER written to – they remain untouched for formulas.
        """
        # Convert item to Title Case
        item = item.title()
        
        # Auto‑generate note if none provided or too short
        if not notes or len(notes.strip()) < 2:
            now = datetime.now()
            notes = f"Added via bot on {now.strftime('%Y-%m-%d %H:%M:%S')}"
        
        sheet = self.sheet_client.get_worksheet("Expense Journal")
        # Find the next empty row in column A (skip header row 1)
        col_a = sheet.col_values(1)  # column A
        next_row = len(col_a) + 1
        if next_row == 1:   # empty sheet? then start from row 2
            next_row = 2

        try:
            # Write each column individually
            sheet.update_cell(next_row, 1, str(year))   # A
            sheet.update_cell(next_row, 2, item)        # B
            sheet.update_cell(next_row, 3, float(amount))  # C
            sheet.update_cell(next_row, 4, str(day))    # D
            sheet.update_cell(next_row, 5, str(month))  # E
            sheet.update_cell(next_row, 8, notes)       # H
            return f"✅ Added expense: {item} for ₹{amount:.2f} on {day}/{month}/{year}. Note: {notes}"
        except Exception as e:
            return f"❌ Failed to add expense: {str(e)}"

    def add_category_mapping(self, item_name, category):
        """
        Add a new row to Item & Category sheet, both stored in Title Case.
        This is used when the user creates a new category during expense addition.
        """
        item_name = item_name.title()
        category = category.title()
        try:
            self.sheet_client.append_row("Item & Category", [item_name, category])
            return f"✅ Added new mapping: '{item_name}' → '{category}'."
        except Exception as e:
            return f"❌ Failed to add category mapping: {str(e)}"