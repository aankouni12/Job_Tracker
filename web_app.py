import os
import secrets
import tempfile
import threading
import time

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from google_auth_oauthlib.flow import Flow

from auth_test import build_service_from_credentials
from extract_applications import run_extraction_core

app = Flask(__name__)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:5000/oauth2callback")
IS_LOCAL_DEV = OAUTH_REDIRECT_URI.startswith("http://localhost") or OAUTH_REDIRECT_URI.startswith("http://127.0.0.1")

# Sessions here only ever hold an opaque job id and an OAuth CSRF/PKCE nonce
# (never credentials — see /oauth2callback), so a rotating key is fine
# security-wise. But set FLASK_SECRET_KEY in production so an in-flight
# sign-in doesn't get invalidated by a restart/redeploy mid-flow; falls
# back to a random per-process key for local dev convenience.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# Cookies should never go out over plain HTTP or be readable/sendable
# cross-site — except SESSION_COOKIE_SECURE has to be off for local
# http://localhost testing, since browsers won't send a Secure cookie over
# plain HTTP at all (which would silently break the whole sign-in flow).
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = not IS_LOCAL_DEV

# Trust X-Forwarded-* headers from the reverse proxies in front of this app
# (the normal setup on most hosting platforms) so OAuth redirect URIs come
# out as https:// instead of http://, and so rate limiting sees each
# visitor's real IP instead of the proxy's. Only enable this if there
# really are trusted proxies in front — otherwise a client could spoof
# these headers. x=2 because Render sits behind Cloudflare (2 hops); adjust
# if you deploy behind a different number of proxies.
if os.environ.get("TRUST_PROXY_HEADERS", "1") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=2, x_host=2, x_for=2)

csrf_limiter_key = get_remote_address
limiter = Limiter(csrf_limiter_key, app=app, default_limits=[])

# Google's OAuth library refuses to exchange a token over plain HTTP by
# default. Only relax that for an explicit localhost redirect URI (local
# dev) — a real deployment must use https:// and should never set this.
if IS_LOCAL_DEV:
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"  # blocks clickjacking the sign-in button
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not IS_LOCAL_DEV:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response

OAUTH_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# How much of a visitor's inbox we scan per run. Kept far smaller than the
# developer's own CLI run (200 emails / 20 location searches) since every
# visitor's run spends the app owner's Anthropic credits, not their own.
PUBLIC_EMAIL_SCAN_COUNT = int(os.environ.get("PUBLIC_EMAIL_SCAN_COUNT", "60"))
PUBLIC_LOCATION_SEARCH_CAP = int(os.environ.get("PUBLIC_LOCATION_SEARCH_CAP", "10"))

# job_id -> {"status": "running"|"done"|"error", "message": str, "file_path": str|None, "error": str|None, "created_at": float}
# In-memory only — fine for a single-process deployment (see README). If you
# scale to multiple worker processes, this needs to move to a shared store
# (e.g. Redis) or jobs started on one worker won't be visible on another.
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_MAX_AGE_SECONDS = 30 * 60  # abandoned jobs (never downloaded) get swept after this


def sweep_stale_jobs():
    """Deletes temp xlsx files and JOBS entries for runs nobody ever came
    back to download — otherwise a visitor who signs in and then closes the
    tab leaves a file on disk and an entry in memory forever."""
    cutoff = time.time() - JOB_MAX_AGE_SECONDS
    with JOBS_LOCK:
        stale_ids = [jid for jid, job in JOBS.items() if job.get("created_at", 0) < cutoff]
        stale_paths = [JOBS[jid].get("file_path") for jid in stale_ids]
        for jid in stale_ids:
            del JOBS[jid]
    for path in stale_paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def build_flow():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set. "
            "Create a Web application OAuth client in Google Cloud Console and set these env vars."
        )
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [OAUTH_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(client_config, scopes=OAUTH_SCOPES, redirect_uri=OAUTH_REDIRECT_URI)


def run_job(job_id, creds_info):
    """Runs entirely in a background thread. creds_info is only ever held
    in this function's local scope — it is never written to disk, JOBS,
    or the session, and goes out of scope (and is garbage-collected) as
    soon as the run finishes."""
    tmp_path = None
    try:
        service = build_service_from_credentials(creds_info)
        fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)

        def progress_cb(msg):
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["message"] = msg

        run_extraction_core(
            service,
            target_email_count=PUBLIC_EMAIL_SCAN_COUNT,
            max_location_searches=PUBLIC_LOCATION_SEARCH_CAP,
            applications_json_path=None,
            applications_xlsx_path=tmp_path,
            location_cache_path="location_cache.json",
            progress_cb=progress_cb,
        )

        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["file_path"] = tmp_path
                JOBS[job_id]["message"] = "Done — your spreadsheet is ready."
    except Exception as e:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login")
@limiter.limit("5 per hour")
def login():
    try:
        flow = build_flow()
    except RuntimeError as e:
        return render_template("index.html", error=str(e)), 500

    # Not passing include_granted_scopes here — we don't want scopes this
    # account granted to some other client under the same project (e.g.
    # gmail.send from the desktop-flow tool) silently tagging along; every
    # visitor's grant should be exactly the scopes we asked for, nothing more.
    auth_url, state = flow.authorization_url()
    session["oauth_state"] = state
    # PKCE: the code_verifier generated for this auth URL must be replayed
    # against the *same* Flow's fetch_token() call, but Flask handles
    # /login and /oauth2callback as separate requests (separate Flow
    # objects) — so it has to be round-tripped through the session, same
    # as the CSRF state.
    session["code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/oauth2callback")
@limiter.limit("10 per hour")
def oauth2callback():
    state = session.pop("oauth_state", None)
    code_verifier = session.pop("code_verifier", None)
    if not state or request.args.get("state") != state:
        return render_template("index.html", error="Sign-in session expired or invalid — please try again."), 400

    if request.args.get("error"):
        return render_template("index.html", error="Sign-in was cancelled."), 400

    # Belt-and-suspenders on top of ProxyFix: force https here specifically,
    # since we already know deterministically (via IS_LOCAL_DEV) whether we
    # should be on https, regardless of whether the proxy hop count above is
    # configured exactly right.
    authorization_response = request.url
    if not IS_LOCAL_DEV and authorization_response.startswith("http://"):
        authorization_response = "https://" + authorization_response[len("http://"):]

    try:
        flow = build_flow()
        flow.code_verifier = code_verifier
        flow.fetch_token(authorization_response=authorization_response)
    except Exception as e:
        return render_template("index.html", error=f"Sign-in failed: {e}"), 400

    creds = flow.credentials

    email = None
    try:
        if creds.id_token:
            info = google_id_token.verify_oauth2_token(
                creds.id_token, google_requests.Request(), GOOGLE_CLIENT_ID
            )
            email = info.get("email")
    except Exception:
        pass

    creds_info = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }

    sweep_stale_jobs()

    job_id = secrets.token_urlsafe(24)
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "message": "Starting…",
            "email": email,
            "file_path": None,
            "error": None,
            "created_at": time.time(),
        }

    threading.Thread(target=run_job, args=(job_id, creds_info), daemon=True).start()

    session["job_id"] = job_id
    return redirect(url_for("status_page", job_id=job_id))


@app.route("/processing/<job_id>")
def status_page(job_id):
    if session.get("job_id") != job_id:
        return redirect(url_for("index"))
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return redirect(url_for("index"))
    return render_template("processing.html", job_id=job_id, email=job.get("email"))


@app.route("/status/<job_id>")
def status(job_id):
    if session.get("job_id") != job_id:
        return jsonify({"status": "error", "message": "Not authorized."}), 403
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"status": "error", "message": "This job was not found or has expired."}), 404
    return jsonify({"status": job["status"], "message": job["message"], "error": job.get("error")})


@app.route("/download/<job_id>")
def download(job_id):
    if session.get("job_id") != job_id:
        return redirect(url_for("index"))

    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job["status"] != "done" or not job.get("file_path"):
            return redirect(url_for("index"))
        path = job["file_path"]
        del JOBS[job_id]  # one-time download

    session.pop("job_id", None)

    response = send_file(path, as_attachment=True, download_name="applications.xlsx")

    @response.call_on_close
    def cleanup():
        try:
            os.remove(path)
        except OSError:
            pass

    return response


if __name__ == "__main__":
    # Debug mode ships an in-browser code console — keep it off unless you
    # explicitly opt in for local development (FLASK_DEBUG=1).
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", "5000")))
