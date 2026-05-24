from config import get_gsheet_client, SHEET_URL
import logging

logger = logging.getLogger(__name__)


class SheetClient:
    def __init__(self):
        self.client = get_gsheet_client()
        self.spreadsheet = self.client.open_by_url(SHEET_URL)

    def get_worksheet(self, name):
        return self.spreadsheet.worksheet(name)

    def append_row(self, sheet_name, row_data):
        # BUG FIX #3: bare print() calls were used for logging — replaced with
        # proper logger calls so output respects the app's log level/handler.
        try:
            logger.debug("Attempting to append to %s: %s", sheet_name, row_data)
            sheet = self.get_worksheet(sheet_name)
            sheet.append_row(row_data, value_input_option='USER_ENTERED')
            logger.debug("Successfully appended to %s", sheet_name)
            return True
        except Exception as e:
            logger.exception("Append to %s failed: %r", sheet_name, e)
            return False

    def update_cell(self, sheet_name, row, col, value):
        sheet = self.get_worksheet(sheet_name)
        sheet.update_cell(row, col, value)
        return True

    def get_all_values(self, sheet_name):
        sheet = self.get_worksheet(sheet_name)
        return sheet.get_all_values()
