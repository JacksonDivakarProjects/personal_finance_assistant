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

    # Return {original-case item: category} using first occurrence of each item
    result = {}
    for _, row in df.iterrows():
        item_raw = str(row['Item name']).strip()
        cat_raw  = str(row['Category']).strip()
        if not item_raw or not cat_raw:
            continue
        if item_raw not in result:
            result[item_raw] = cat_raw
    return result


def load_budget(sheet_client, sheet_name):
    all_data = sheet_client.get_all_values(sheet_name)
    if len(all_data) < 2:
        return {}

    headers = [h.strip() for h in all_data[0][:2]]   # FIX issue #8: strip headers
    rows = [row[:2] for row in all_data[1:]]
    df = pd.DataFrame(rows, columns=headers)

    df = df[df['Category'].str.strip() != '']
    df = df[df['Category'].str.strip() != 'Total']

    df['Amount'] = (
        df['Amount']
        .astype(str)
        .str.replace('₹', '', regex=False)
        .str.replace(',', '', regex=False)
        .str.strip()
    )
    df['Amount'] = pd.to_numeric(df['Amount'], errors='coerce')
    df = df.dropna(subset=['Amount'])
    return df.set_index('Category')['Amount'].to_dict()


def get_actual_spending(expense_df, item_to_cat):
    expense_df = expense_df.copy()

    if expense_df.empty:
        return {}, 0.0

    # FIX (issue #9): gspread returns empty cells as '' (empty string), not NaN.
    # fillna() only fills NaN — it silently skips '' cells, so items that have
    # an empty Category column in the sheet are excluded from actual spend even
    # if they're mapped in item_to_cat.
    # Fix: replace '' with NaN first, then fillna from the item→category mapping.
    if 'Category' in expense_df.columns:
        expense_df['Category'] = expense_df['Category'].replace('', pd.NA)
        expense_df['Category'] = expense_df['Category'].fillna(
            expense_df['Item'].map(item_to_cat)
        )
    else:
        expense_df['Category'] = expense_df['Item'].map(item_to_cat)

    expense_df = expense_df.dropna(subset=['Category'])
    actual = expense_df.groupby('Category')['Amount (₹)'].sum().to_dict()
    total  = sum(actual.values())
    return actual, total
