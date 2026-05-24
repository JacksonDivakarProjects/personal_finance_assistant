import os
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
import gspread

load_dotenv()

# Google Sheets API scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

ACCOUNT_FILE = os.getenv("ACCOUNT_FILE")

# BUG FIX #1: ACCOUNT_FILE was never validated — if missing, gspread raises a
# cryptic FileNotFoundError deep in the stack. Fail fast with a clear message.
if not ACCOUNT_FILE:
    raise ValueError("Missing ACCOUNT_FILE in .env file")

def get_gsheet_client():
    creds = Credentials.from_service_account_file(ACCOUNT_FILE, scopes=SCOPES)
    return gspread.authorize(creds)

SHEET_URL = os.getenv("SHEET_URL")

# BUG FIX #2: env var was "sheet_url" (lowercase) in config but the .env
# convention and docker-compose would set it as SHEET_URL (uppercase).
# Normalised to SHEET_URL; also add a fallback to the old lowercase key so
# existing .env files still work without changes.
if not SHEET_URL:
    SHEET_URL = os.getenv("sheet_url")
if not SHEET_URL:
    raise ValueError("Missing SHEET_URL (or sheet_url) in .env file")

# Groq LLM
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.3-70b-versatile")
