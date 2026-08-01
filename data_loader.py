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


def get_actual_spending(expense_df, item_to_cat):
    expense_df = expense_df.copy()

    if expense_df.empty:
        return {}, 0.0

    # FIX (issue #9): gspread returns empty cells as '' (empty string), not NaN.
    # fillna() only fills NaN — it silently skips '' cells, so items that have
    # an empty Category column in the sheet are excluded from actual spend even
    # if they're mapped in item_to_cat.
    # Fix: replace '' with NaN first, then fillna from the item→category mapping.
    #
    # FIX (issue #11): item_to_cat.get()/dict.map() lookups are case-sensitive,
    # but every other lookup in this app (_item_already_mapped, the fuzzy
    # matcher) treats item names case-insensitively. An expense logged as
    # "coffee" would fail to match a mapping keyed "Coffee", leaving Category
    # as NaN and getting silently dropped by dropna() below — understating
    # total spend with no error or warning. Build a lowercase-keyed lookup so
    # the mapping matches regardless of case.
    item_to_cat_lower = {str(k).lower(): v for k, v in item_to_cat.items()}
    if 'Category' in expense_df.columns:
        expense_df['Category'] = expense_df['Category'].replace('', pd.NA)
        expense_df['Category'] = expense_df['Category'].fillna(
            expense_df['Item'].str.lower().map(item_to_cat_lower)
        )
    else:
        expense_df['Category'] = expense_df['Item'].str.lower().map(item_to_cat_lower)

    expense_df = expense_df.dropna(subset=['Category'])
    actual = expense_df.groupby('Category')['Amount (₹)'].sum().to_dict()
    total  = sum(actual.values())
    return actual, total
