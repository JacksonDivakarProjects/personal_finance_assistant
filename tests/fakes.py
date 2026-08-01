# tests/fakes.py
"""
In-memory fakes used by the offline regression suite.

These deliberately mimic the *real* method surface of SheetClient (get_worksheet,
append_row, update_cell, get_all_values) and the gspread Worksheet object
(col_values, update, update_cell, get_all_records) so that the REAL
DataWriter / data_loader code runs unmodified against them. Nothing here ever
touches the network, account.json, or the live spreadsheet.
"""

import re
from typing import List, Optional


def _col_letters_to_num(letters: str) -> int:
    num = 0
    for ch in letters:
        num = num * 26 + (ord(ch.upper()) - ord('A') + 1)
    return num


def _parse_range(range_notation: str):
    m = re.match(r'^([A-Za-z]+)(\d+):([A-Za-z]+)(\d+)$', range_notation)
    if not m:
        raise ValueError(f"Bad range notation: {range_notation!r}")
    c1, r1, c2, r2 = m.groups()
    return _col_letters_to_num(c1), int(r1), _col_letters_to_num(c2), int(r2)


class FakeWorksheet:
    """Mimics the subset of gspread.Worksheet that data_writer/data_loader use."""

    def __init__(self, client: "FakeSheetClient", name: str):
        self.client = client
        self.name = name

    def col_values(self, col_idx: int) -> List[str]:
        rows = self.client.store.get(self.name, [])
        return [(row[col_idx - 1] if len(row) >= col_idx else '') for row in rows]

    def get_all_values(self) -> List[list]:
        return self.client.store.get(self.name, [])

    def update(self, range_notation: str, values, value_input_option=None):
        self.client.update_calls.append((self.name, range_notation, values, value_input_option))
        start_col, start_row, _end_col, _end_row = _parse_range(range_notation)
        rows = self.client.store.setdefault(self.name, [])
        for r_offset, row_values in enumerate(values):
            row_idx = start_row + r_offset
            while len(rows) < row_idx:
                rows.append([])
            row = rows[row_idx - 1]
            for c_offset, v in enumerate(row_values):
                col_idx = start_col + c_offset
                while len(row) < col_idx:
                    row.append('')
                row[col_idx - 1] = v

    def update_cell(self, row: int, col: int, value):
        self.client.worksheet_update_cell_calls.append((self.name, row, col, value))
        rows = self.client.store.setdefault(self.name, [])
        while len(rows) < row:
            rows.append([])
        r = rows[row - 1]
        while len(r) < col:
            r.append('')
        r[col - 1] = value

    def get_all_records(self):
        rows = self.client.store.get(self.name, [])
        if not rows:
            return []
        headers = rows[0]
        records = []
        for row in rows[1:]:
            rec = {}
            for i, h in enumerate(headers):
                rec[h] = row[i] if i < len(row) else ''
            records.append(rec)
        return records


class FakeSheetClient:
    """
    Faithful in-memory stand-in for sheet_client.SheetClient. Stores worksheet
    data in a plain dict-of-lists-of-lists so tests can both seed initial state
    and assert on exactly what got written (column order, types, row counts).

    NEVER constructs a real gspread client, never touches account.json/SHEET_URL.
    """

    def __init__(self):
        self.store = {}
        self.update_calls = []                 # worksheet.update(range, values, ...)
        self.worksheet_update_cell_calls = []   # worksheet.update_cell(row, col, value)
        self.append_row_calls = []              # SheetClient.append_row(sheet_name, row)
        self.update_cell_calls = []             # SheetClient.update_cell(sheet_name, row, col, value)

    def get_worksheet(self, name):
        return FakeWorksheet(self, name)

    def append_row(self, sheet_name, row_data):
        self.append_row_calls.append((sheet_name, list(row_data)))
        self.store.setdefault(sheet_name, []).append(list(row_data))
        return True

    def update_cell(self, sheet_name, row, col, value):
        self.update_cell_calls.append((sheet_name, row, col, value))
        self.get_worksheet(sheet_name).update_cell(row, col, value)
        return True

    def get_all_values(self, sheet_name):
        return self.store.get(sheet_name, [])


def make_fake_sheet_client(item_category_rows: Optional[List[list]] = None) -> FakeSheetClient:
    """Build a FakeSheetClient pre-seeded with minimally valid sheet headers."""
    client = FakeSheetClient()
    client.store["Expense Journal"] = [
        ["Year", "Item", "Amount (₹)", "Day", "Month", "Col6", "Col7", "Notes"]
    ]
    client.store["Item & Category"] = item_category_rows or [["Item name", "Category"]]
    client.store["Category Budget"] = [["Category", "Amount"]]
    return client


class FakeLLMResponse:
    def __init__(self, content: str):
        self.content = content


class ScriptedLLM:
    """
    Stand-in for agent.llm. `.invoke(prompt)` pops the next scripted item and
    either returns a FakeLLMResponse(content) or raises it (if it's an
    exception instance/class). Raises AssertionError if called more times
    than scripted, so tests fail loudly instead of silently hitting the real
    Groq API via some un-mocked path.
    """

    def __init__(self, contents=None):
        self._contents = list(contents or [])
        self.calls = []  # list of prompts passed to invoke()

    def invoke(self, prompt):
        self.calls.append(prompt)
        if not self._contents:
            raise AssertionError(
                f"ScriptedLLM.invoke called more times than scripted "
                f"(call #{len(self.calls)}). Prompt was:\n{prompt}"
            )
        item = self._contents.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item("scripted failure")
        return FakeLLMResponse(item)
