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

# Load credentials and client
def get_gsheet_client():
    creds = Credentials.from_service_account_file("account.json", scopes=SCOPES)
    return gspread.authorize(creds)

SHEET_URL = os.getenv("sheet_url")
if not SHEET_URL:
    raise ValueError("Missing sheet_url in .env file")

# Groq LLM
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.3-70b-versatile")