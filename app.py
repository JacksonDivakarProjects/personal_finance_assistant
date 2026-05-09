from flask import Flask
import os

app = Flask(__name__)

@app.route("/")
def home():
    return "Finance bot running"

def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))