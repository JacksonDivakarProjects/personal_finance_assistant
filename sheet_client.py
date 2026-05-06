from config import get_gsheet_client, SHEET_URL

class SheetClient:
    def __init__(self):
        self.client = get_gsheet_client()
        self.spreadsheet = self.client.open_by_url(SHEET_URL)

    def get_worksheet(self, name):
        return self.spreadsheet.worksheet(name)

    def append_row(self, sheet_name, row_data):
        try:
            # Print exactly what you're trying to send
            print(f"Attempting to append to {sheet_name}: {row_data}")
            sheet = self.get_worksheet(sheet_name)
            sheet.append_row(row_data, value_input_option='USER_ENTERED')
            print(f"✅ Successfully appended to {sheet_name}")
            return True
            
        except Exception as e:
            # This will print the real reason for failure
            print(f"❌ Append failed: {repr(e)}")
            import traceback
            traceback.print_exc()
            return False

    def update_cell(self, sheet_name, row, col, value):
        sheet = self.get_worksheet(sheet_name)
        sheet.update_cell(row, col, value)
        return True

    def get_all_values(self, sheet_name):
        sheet = self.get_worksheet(sheet_name)
        return sheet.get_all_values()