from threading import Thread
from app import run_web
from bot import main as run_bot

# BUG FIX #20: the web thread was started as a non-daemon thread. If the bot
# crashes, the process hangs forever waiting for the web thread to finish.
# Mark it as a daemon so it exits automatically when the main thread ends.
web_thread = Thread(target=run_web, daemon=True)
web_thread.start()

run_bot()
