# Job Application Tracker

Scans your Gmail inbox for job-application-related emails, uses Claude to classify and
extract structured data from them (company, role, status, location, etc.), deduplicates
repeat mentions of the same application, looks up the specific job-posting location for
records missing one, and writes everything to an Excel spreadsheet — with a small web page
that emails the spreadsheet to whoever asks for it.

## How it works

| File | Purpose |
|---|---|
| `auth_test.py` | Gmail OAuth. Requests `gmail.readonly` (scan the inbox) and `gmail.send` (email the spreadsheet out), and re-triggers the consent flow automatically if the saved token is missing a scope it needs. |
| `fetch_job_emails.py` | Searches Gmail for candidate emails (applications, interviews, rejections, etc.) via a keyword query, paginating until enough candidates are collected. |
| `extract_applications.py` | The main pipeline: classifies each candidate email with Claude Haiku, dedupes repeat mentions of the same company, looks up missing locations via web search for a capped number of records per run, and writes `applications.json` / `applications.xlsx`. |
| `mailer.py` | Builds a MIME email with `applications.xlsx` attached and sends it via the Gmail API. |
| `web_app.py` | A small Flask app: a form asks for an email address and sends the current spreadsheet there. |
| `templates/index.html` | The form page. |
| `location_cache.json` | Cache of `company + role -> location` lookups so repeat runs never re-search the same job. |
| `applications.json` / `applications.xlsx` | The current output — one row per deduplicated application. |

## Setup

### 1. Install dependencies

```
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 2. Google Cloud / Gmail API

1. In [Google Cloud Console](https://console.cloud.google.com), create (or reuse) a project and enable the **Gmail API**.
2. Under **APIs & Services → OAuth consent screen**, add both scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.send`
3. While the app is in **Testing** status, add your own Gmail address under **Test users** — only listed test users can complete the consent screen.
4. Create an OAuth client ID (type **Desktop app**), download it, and save it as `credentials.json` in the project root.
5. The first time anything needs Gmail access (running `extract_applications.py`, or clicking "Send" in the web app), a browser window will open asking you to sign in and approve access. This writes `token.json`, which is reused (and auto-refreshed) after that.

### 3. Anthropic API key

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

`credentials.json`, `token.json`, and `.env` are all gitignored — never commit them.

## Usage

### Build/update the spreadsheet

```
python extract_applications.py
```

This pulls recent candidate emails from Gmail, classifies them with Claude, dedupes by
company, and fills in missing locations (capped at 20 web searches per run, prioritizing
active applications and interview invites — rerun it to pick up more over time). Progress
is saved incrementally to `applications.json`, so an interrupted run doesn't lose work; the
final spreadsheet is written to `applications.xlsx`.

### Run the "email me the sheet" web page

```
python web_app.py
```

Visit `http://127.0.0.1:5000`, enter an email address, and the current `applications.xlsx`
is sent there as an attachment.

## Notes

- **Credits**: location lookups use Claude with web search + web fetch, which costs more
  per call than plain classification. Per-run search count is capped, fetched page content
  is capped, and results are cached — rerun the extraction script to gradually fill in more
  locations without re-paying for ones already found.
- **Security**: the web app has CSRF protection, rate limiting (5 sends/hour), and debug
  mode is off by default (`FLASK_DEBUG=1` to opt in for local development). It's a local
  dev server — if you ever want to expose it beyond your own machine, it needs a real WSGI
  server and HTTPS in front of it first.
- **Testing-mode OAuth app**: only Google accounts added as test users in the OAuth consent
  screen can complete Gmail authorization for this app.
