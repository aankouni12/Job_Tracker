# Job Application Tracker

Scans a Gmail inbox for job-application emails, classifies them with Claude, dedupes
repeat mentions of the same application, fills in missing job locations, and produces an
Excel spreadsheet.

**Live at:** https://job-tracker-32mw.onrender.com — sign in with Google to get a
spreadsheet built from your own inbox. Only Google accounts added as test users (Cloud
Console → OAuth consent screen → Audience) can sign in.

## Files

- `extract_applications.py` — the pipeline (classify → dedupe → locate → write). Run
  directly for your own account (200 emails, `applications.json`/`applications.xlsx`).
- `fetch_job_emails.py`, `auth_test.py`, `mailer.py` — Gmail search, auth, and email-sending
  helpers used by the pipeline above.
- `web_app.py` — the public Flask app: Google sign-in per visitor, background extraction,
  direct download. Each visitor's run is capped (60 emails, 10 location searches) since it
  spends your Anthropic credits.
- `templates/` — sign-in page and progress-polling page for the web app.

## Setup

```
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
```

**Google Cloud** (one project, two OAuth clients — enable the Gmail API first):
- *Desktop client* (for your own `extract_applications.py` runs): create an OAuth client,
  type **Desktop app**, save as `credentials.json`. Add `gmail.readonly` + `gmail.send`
  scopes, add yourself as a test user.
- *Web client* (for `web_app.py`): create an OAuth client, type **Web application**, add
  redirect URI(s) like `http://localhost:5050/oauth2callback`. Add `gmail.readonly` scope,
  and add every person who should be able to sign in as a test user.

**`.env`:**
```
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_OAUTH_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=...
OAUTH_REDIRECT_URI=http://localhost:5050/oauth2callback
```

## Usage

```
python extract_applications.py   # build/update your own spreadsheet
python web_app.py                # run the public web app locally
```

## Deployment

Live on Render's free tier. Build command `pip install -r requirements.txt`, start command
`waitress-serve --host=0.0.0.0 --port=$PORT web_app:app`, env vars as above plus
`OAUTH_REDIRECT_URI` set to the real `https://...onrender.com/oauth2callback` (register the
same URL in Google Cloud Console). Staying in Testing mode (vs. Google's full verification)
is the right call unless you need more than 100 users or want the "unverified app" warning
gone — verification for a restricted scope like `gmail.readonly` can mean a real security
assessment.

## Security

- OAuth Authorization Code flow with PKCE + CSRF `state`; each visitor's tokens live only in
  memory for the duration of their run, never written to disk/session/logs.
- `/status/<job_id>` and `/download/<job_id>` are session-scoped to the job's owner; job IDs
  are 192-bit random. Downloaded files are deleted immediately; abandoned jobs are swept
  after 30 minutes.
- Web app only requests `gmail.readonly` + identity — never `gmail.send`.
- Rate-limited sign-in endpoints; `ProxyFix` trusts 2 hops (Render sits behind Cloudflare) so
  rate limiting sees real visitor IPs, not the proxy's.
- Secure/HttpOnly/SameSite cookies, security headers (HSTS, X-Frame-Options, nosniff).
- `processing.html` renders any text derived from a visitor's email content via
  `textContent`, never `innerHTML` — that text is untrusted input.
- `credentials.json`, `token.json`, `.env`, `applications.json`/`.xlsx`, `location_cache.json`
  are all gitignored — never commit them.
