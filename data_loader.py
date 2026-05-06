import pandas as pd
from sheet_client import SheetClient

def load_expense_journal(sheet_client):
    """Load Expense Journal: first 8 columns, clean amounts."""
    all_data = sheet_client.get_all_values("Expense Journal")
    if len(all_data) < 2:
        return pd.DataFrame()
    headers = all_data[0][:8]
    rows = [row[:8] for row in all_data[1:]]
    df = pd.DataFrame(rows, columns=headers)
    df = df[df['Item'].notna() & (df['Item'] != '')]
    df['Amount (₹)'] = df['Amount (₹)'].astype(str).str.replace('₹', '').str.replace(',', '').str.strip()
    df['Amount (₹)'] = pd.to_numeric(df['Amount (₹)'], errors='coerce')
    df = df.dropna(subset=['Amount (₹)'])
    return df

def load_item_category(sheet_client):
    records = sheet_client.get_worksheet("Item & Category").get_all_records()
    if not records:
        return {}
    df = pd.DataFrame(records)
    df.columns = [c.strip() for c in df.columns]
    return df.set_index('Item name')['Category'].to_dict()

def load_budget(sheet_client, sheet_name):
    all_data = sheet_client.get_all_values(sheet_name)
    if len(all_data) < 2:
        return {}
    headers = all_data[0][:2]
    rows = [row[:2] for row in all_data[1:]]
    df = pd.DataFrame(rows, columns=headers)
    df = df[df['Category'].notna() & (df['Category'].str.strip() != '')]
    df = df[df['Category'].str.strip() != 'Total']
    df['Amount'] = df['Amount'].astype(str).str.replace('₹', '').str.replace(',', '').str.strip()
    df['Amount'] = pd.to_numeric(df['Amount'], errors='coerce')
    df = df.dropna(subset=['Amount'])
    return df.set_index('Category')['Amount'].to_dict()

def get_actual_spending(expense_df, item_to_cat):
    if 'Category' in expense_df.columns:
        expense_df['Category'] = expense_df['Category'].fillna(expense_df['Item'].map(item_to_cat))
    else:
        expense_df['Category'] = expense_df['Item'].map(item_to_cat)
    expense_df = expense_df.dropna(subset=['Category'])
    actual = expense_df.groupby('Category')['Amount (₹)'].sum().to_dict()
    total = sum(actual.values())
    return actual, total