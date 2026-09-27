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

# Google service-account credentials, read from env vars instead of an
# account.json key file (copy the matching fields out of the downloaded JSON).
GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY")
GOOGLE_PRIVATE_KEY_ID = os.getenv("GOOGLE_PRIVATE_KEY_ID")
GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID")
GOOGLE_TOKEN_URI = os.getenv("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token")

# BUG FIX #1: credentials were never validated — if missing, google-auth raises
# a cryptic error deep in the stack. Fail fast with a clear message.
if not GOOGLE_CLIENT_EMAIL or not GOOGLE_PRIVATE_KEY:
    raise ValueError("Missing GOOGLE_CLIENT_EMAIL or GOOGLE_PRIVATE_KEY in .env file")

# .env files hold the PEM key on one line with literal "\n" sequences —
# convert them back to real newlines or google-auth rejects the key.
GOOGLE_PRIVATE_KEY = GOOGLE_PRIVATE_KEY.replace("\\n", "\n")

def get_gsheet_client():
    creds = Credentials.from_service_account_info(
        {
            "type": "service_account",
            "client_email": GOOGLE_CLIENT_EMAIL,
            "private_key": GOOGLE_PRIVATE_KEY,
            "private_key_id": GOOGLE_PRIVATE_KEY_ID,
            "project_id": GOOGLE_PROJECT_ID,
            "token_uri": GOOGLE_TOKEN_URI,
        },
        scopes=SCOPES,
    )
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
