from flask import Flask
import os

app = Flask(__name__)


@app.route("/")
def home():
    return "Finance bot running"


# BUG FIX #19: app.py had no __name__ guard, so importing it from main.py
# caused run_web() to be defined at module level — harmless here — but if
# anyone ever ran `python app.py` directly the server would NOT start because
# run_web() was never called.  Added a standard entry-point guard.
def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    run_web()
