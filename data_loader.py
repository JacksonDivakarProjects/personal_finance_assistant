import pandas as pd
from sheet_client import SheetClient


def load_expense_journal(sheet_client):
    """Load Expense Journal: first 8 columns, clean amounts."""
    all_data = sheet_client.get_all_values("Expense Journal")
    if len(all_data) < 2:
        return pd.DataFrame()

    headers = all_data[0][:8]
    # FIX (issue #8): strip column header names so leading/trailing spaces
    # in the sheet header row don't cause KeyError on 'Item' or 'Amount (₹)'.
    headers = [h.strip() for h in headers]

    rows = [row[:8] for row in all_data[1:]]
    df = pd.DataFrame(rows, columns=headers)

    df = df[df['Item'].str.strip() != '']

    df['Amount (₹)'] = (
        df['Amount (₹)']
        .astype(str)
        .str.replace('₹', '', regex=False)
        .str.replace(',', '', regex=False)
        .str.strip()
    )
    df['Amount (₹)'] = pd.to_numeric(df['Amount (₹)'], errors='coerce')
    df = df.dropna(subset=['Amount (₹)'])
    return df


def load_item_category(sheet_client):
    """
    Load Item & Category sheet.

    FIX (issue #4): the original set_index().to_dict() silently drops
    duplicate item names, keeping only the last one.  Instead, build the
    dict manually so the FIRST mapping wins (preserving original intent)
    and duplicates are visible in the log.

    FIX (issue #23): this used to dedupe into a lowercase-keyed `mapping`
    dict internally, then discard it and rebuild an exact-case-keyed
    `result` as the actual return value — so case-variant duplicates like
    "Coffee" and "COFFEE" survived as two separate entries, each able to map
    to a *different* category. Every case-insensitive lookup elsewhere
    (`_item_already_mapped`, `get_actual_spending`'s `item_to_cat_lower`)
    then had to silently collapse them again, non-deterministically, with no
    logging. Return the already-deduped lowercase mapping directly instead.
    """
    records = sheet_client.get_worksheet("Item & Category").get_all_records()
    if not records:
        return {}

    import logging
    logger = logging.getLogger(__name__)

    df = pd.DataFrame(records)
    df.columns = [c.strip() for c in df.columns]

    if 'Item name' not in df.columns or 'Category' not in df.columns:
        return {}

    mapping = {}
    for _, row in df.iterrows():
        item_raw = str(row['Item name']).strip()
        cat_raw  = str(row['Category']).strip()
        if not item_raw or not cat_raw:
            continue
        key = item_raw.lower()
        if key in mapping:
            logger.debug("Duplicate item mapping ignored: '%s' -> '%s' (keeping '%s')",
                         item_raw, cat_raw, mapping[key])
        else:
            mapping[key] = cat_raw   # store lowercase key, original-case value

    return mapping


def load_budget(sheet_client, sheet_name):
    all_data = sheet_client.get_all_values(sheet_name)
    if len(all_data) < 2:
        return {}

    headers = [h.strip() for h in all_data[0][:2]]   # FIX issue #8: strip headers
    rows = [row[:2] for row in all_data[1:]]
    df = pd.DataFrame(rows, columns=headers)

    # FIX (issue #24): the dedupe loop below keys directly on df['Category'],
    # but this column was never stripped of leading/trailing whitespace (only
    # checked with a throwaway .str.strip() in the filters, not reassigned) —
    # a stray trailing-space cell (e.g. "Groceries ") survived as a distinct
    # key from "Groceries", so it dodged the dedupe rule and could sit
    # invisibly under a whitespace-suffixed key that the rest of the app
    # (whose category strings ARE stripped) never matches against.
    df['Category'] = df['Category'].str.strip()

    df = df[df['Category'] != '']
    df = df[df['Category'] != 'Total']

    df['Amount'] = (
        df['Amount']
        .astype(str)
        .str.replace('₹', '', regex=False)
        .str.replace(',', '', regex=False)
        .str.strip()
    )
    df['Amount'] = pd.to_numeric(df['Amount'], errors='coerce')
    df = df.dropna(subset=['Amount'])

    # FIX (issue #12): set_index().to_dict() silently keeps the LAST row for a
    # duplicated category, inconsistent with load_item_category's issue #4 fix
    # (which keeps the first and logs the duplicate). Apply the same rule here
    # so a repeated category row in the Budget sheet doesn't silently clobber
    # an earlier budget figure with no warning.
    import logging
    logger = logging.getLogger(__name__)
    budget = {}
    for cat, amt in zip(df['Category'], df['Amount']):
        if cat in budget:
            logger.debug("Duplicate budget category ignored: '%s' -> %s (keeping %s)",
                         cat, amt, budget[cat])
        else:
            budget[cat] = amt
    return budget


def load_summary_table(sheet_client):
    """
    Load the "Summary Table" sheet's category-wise pivot into a
    {category: amount} dict.

    This is a native Google Sheets pivot table (Category / SUM of Amount (₹)),
    not a plain 2-column sheet like Category Budget, so its header text isn't
    a fixed "Amount" — the first two columns are read positionally instead of
    by header name. A blank-category row (the pivot's bucket for any
    uncategorized expense rows) and the "Grand Total" row are skipped; the
    same first-wins duplicate rule as load_budget/load_item_category applies.

    Deliberately does NOT attempt to read the sheet's Income/Gap/Remaining
    side panel (columns further right, e.g. col I in practice) — that panel's
    position isn't tied to the pivot's header/row structure the way this
    Category/Amount pair is, so parsing it by fixed row offsets would be
    guessing at a layout rather than reading a real table.
    """
    all_data = sheet_client.get_all_values("Summary Table")
    if len(all_data) < 2:
        return {}

    rows = [row[:2] for row in all_data[1:]]
    df = pd.DataFrame(rows, columns=['Category', 'Amount'])

    # The pivot's blank-category bucket represents real Expense Journal rows
    # whose Category cell was empty (e.g. an unmapped item) — it can carry a
    # genuinely nonzero amount, so it's folded into 'Uncategorized' rather
    # than dropped outright; dropping it would silently understate this
    # dict's total relative to the sheet's own Grand Total with no way for a
    # caller to detect the gap.
    df['Category'] = df['Category'].str.strip().replace('', 'Uncategorized')
    df = df[df['Category'].str.lower() != 'grand total']

    df['Amount'] = (
        df['Amount']
        .astype(str)
        .str.replace('₹', '', regex=False)
        .str.replace(',', '', regex=False)
        .str.strip()
    )
    df['Amount'] = pd.to_numeric(df['Amount'], errors='coerce')
    df = df.dropna(subset=['Amount'])

    import logging
    logger = logging.getLogger(__name__)
    summary = {}
    for cat, amt in zip(df['Category'], df['Amount']):
        if cat in summary:
            logger.debug("Duplicate summary-table category ignored: '%s' -> %s (keeping %s)",
                         cat, amt, summary[cat])
        else:
            summary[cat] = amt
    return summary


def _resolve_categories(expense_df, item_to_cat):
    """
    Return a copy of expense_df with its 'Category' column filled in from
    item_to_cat wherever the sheet's own Category cell is blank/missing.

    FIX (issue #9): gspread returns empty cells as '' (empty string), not NaN.
    fillna() only fills NaN — it silently skips '' cells, so items that have
    an empty Category column in the sheet are excluded unless we replace ''
    with NaN first, then fillna from the item→category mapping.

    FIX (issue #11): item_to_cat.get()/dict.map() lookups are case-sensitive,
    but every other lookup in this app (_item_already_mapped, the fuzzy
    matcher) treats item names case-insensitively. An expense logged as
    "coffee" would fail to match a mapping keyed "Coffee", leaving Category
    as NaN — understating total spend with no error or warning. Build a
    lowercase-keyed lookup so the mapping matches regardless of case.
    """
    expense_df = expense_df.copy()
    item_to_cat_lower = {str(k).lower(): v for k, v in item_to_cat.items()}
    if 'Category' in expense_df.columns:
        expense_df['Category'] = expense_df['Category'].replace('', pd.NA)
        expense_df['Category'] = expense_df['Category'].fillna(
            expense_df['Item'].str.lower().map(item_to_cat_lower)
        )
    else:
        expense_df['Category'] = expense_df['Item'].str.lower().map(item_to_cat_lower)
    return expense_df


def get_actual_spending(expense_df, item_to_cat):
    if expense_df.empty:
        return {}, 0.0

    resolved = _resolve_categories(expense_df, item_to_cat).dropna(subset=['Category'])
    actual = resolved.groupby('Category')['Amount (₹)'].sum().to_dict()
    total  = sum(actual.values())
    return actual, total


def get_expense_records(expense_df, item_to_cat, limit=None):
    """
    Return per-record expense data (date, item, amount, category), sorted
    most-recent first, for record-wise / time-scoped query answering (e.g.
    "how much did I spend last week").

    The date is built from the Year/Month/Day columns rather than any
    formula-derived sheet column: those three are written by data_writer.py
    as plain numbers, so combining them is unambiguous, whereas a sheet
    formula's display format isn't guaranteed and shouldn't be parsed as a
    data source. (Confirmed via live-sheet inspection: the Expense Journal's
    column F is actually blank in practice — CLAUDE.md's "F: formula-derived
    Date" description does not hold, so Year/Month/Day is the only reliable
    source of a per-row date.)

    Rows whose Year/Month/Day don't combine into a valid date are dropped
    rather than raising, since this feeds a best-effort query answer, not a
    write path.
    """
    if expense_df.empty:
        return []

    resolved = _resolve_categories(expense_df, item_to_cat)
    resolved['Category'] = resolved['Category'].fillna('Uncategorized')

    for col in ('Year', 'Month', 'Day'):
        if col not in resolved.columns:
            return []
        resolved[col] = pd.to_numeric(resolved[col], errors='coerce')

    resolved['_ParsedDate'] = pd.to_datetime(
        dict(year=resolved['Year'], month=resolved['Month'], day=resolved['Day']),
        errors='coerce'
    )
    resolved = resolved.dropna(subset=['_ParsedDate']).sort_values('_ParsedDate', ascending=False)

    records = [
        {
            "date":     row['_ParsedDate'].strftime('%Y-%m-%d'),
            "item":     row['Item'],
            "amount":   float(row['Amount (₹)']),
            "category": row['Category'],
        }
        for _, row in resolved.iterrows()
    ]
    return records[:limit] if limit is not None else records
