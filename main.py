from threading import Thread
from app import run_web
from bot import main as run_bot

Thread(target=run_web).start()

run_bot()