import os
import re
import secrets

from flask import Flask, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect

from mailer import send_spreadsheet

app = Flask(__name__)
# New random key per process start. CSRF tokens are only ever used within a
# single page-load -> submit cycle, so this doesn't need to persist.
app.secret_key = secrets.token_hex(32)

csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=[])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CONTROL_CHARS = set("\r\n\0")


def is_valid_email(value):
    if any(c in CONTROL_CHARS for c in value):
        return False
    return bool(EMAIL_RE.match(value))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/send", methods=["POST"])
@limiter.limit("5 per hour")
def send():
    email = (request.form.get("email") or "").strip()

    if not is_valid_email(email):
        return render_template("index.html", error="Please enter a valid email address."), 400

    if not os.path.exists("applications.xlsx"):
        return render_template(
            "index.html",
            error="No spreadsheet found yet — run extract_applications.py first.",
        ), 500

    try:
        send_spreadsheet(email)
    except Exception as e:
        return render_template("index.html", error=f"Failed to send email: {e}"), 500

    return render_template("index.html", success=f"Sent! Check {email} for your spreadsheet.")


if __name__ == "__main__":
    # Debug mode ships an in-browser code console — keep it off unless you
    # explicitly opt in for local development (FLASK_DEBUG=1).
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=5000)
