# test_bot.py - Simple test for sheet read/write operations
from sheet_client import SheetClient
from data_loader import load_expense_journal, load_item_category, get_actual_spending
from data_writer import DataWriter
from datetime import datetime

def main():
    print("🔧 Starting simple test...")
    client = SheetClient()
    writer = DataWriter(client)
    
    # 1. Read existing data
    print("\n1. Reading existing Expense Journal...")
    df = load_expense_journal(client)
    print(f"   Found {len(df)} expense rows.")
    if len(df) > 0:
        print("   First 3 rows:")
        print(df.head(3)[['Item', 'Amount (₹)', 'Category']])
    
    print("\n2. Reading Item & Category mapping...")
    mapping = load_item_category(client)
    print(f"   Loaded {len(mapping)} mappings.")
    print("   Sample:", dict(list(mapping.items())[:3]))
    
    # 3. Test adding an expense with plain number amount
    print("\n3. Adding a test expense...")
    now = datetime.now()
    result = writer.add_expense(
        year=now.year,
        item="TestItem123",
        amount=99.99,
        day=now.day,
        month=now.month,
        notes="Test from script"
    )
    print(f"   Result: {result}")
    
    # 4. Optionally add a new category mapping (if needed)
    if "TestItem123" not in mapping:
        print("\n4. Adding category mapping for TestItem123...")
        add_map = writer.add_category_mapping("TestItem123", "TestCategory")
        print(f"   {add_map}")
    else:
        print("\n4. TestItem123 already has category, skipping mapping.")
    
    # 5. Reload data and verify the new expense appears
    print("\n5. Reloading data to verify...")
    df2 = load_expense_journal(client)
    new_rows = df2[df2['Item'] == "TestItem123"]
    if len(new_rows) > 0:
        print(f"   ✅ Test expense found! Amount: {new_rows.iloc[0]['Amount (₹)']}")
        print(f"   Date column (should be blank in raw data): '{new_rows.iloc[0].get('Date', '')}'")
        print("   Note: Date/Category are blank because formulas fill them in the sheet.")
    else:
        print("   ❌ Test expense not found – something went wrong.")
    
    # 6. Show actual vs budget summary
    print("\n6. Actual spending summary (categories):")
    actual, total = get_actual_spending(df2, mapping)
    for cat, amt in sorted(actual.items())[:5]:
        print(f"   {cat}: ₹{amt:.2f}")
    if len(actual) > 5:
        print(f"   ... and {len(actual)-5} more categories.")
    print(f"   Total: ₹{total:.2f}")
    
    print("\n✅ Test completed. Check your Google Sheet – a new row with 'TestItem123' should appear at the bottom (Year, Item, Amount (number), Day, Month filled; Date and Category blank).")

if __name__ == "__main__":
    main()