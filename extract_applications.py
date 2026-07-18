import json
import os
import re
import sys
from dotenv import load_dotenv
from anthropic import Anthropic
from openpyxl import Workbook
from fetch_job_emails import fetch_candidate_emails
from auth_test import get_gmail_service

# Windows consoles default to cp1252, which can't encode the ✓/— characters
# used in progress output below.
sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def get_email_body(service, msg_id):
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()
    return msg.get("snippet", "")


def extract_with_claude(email):
    prompt = f"""You are reviewing an email from the inbox of someone who has been actively applying to jobs. Determine if this email is a DIRECT, PERSONAL confirmation related to an application THEY submitted — not a job suggestion, alert, or aggregator notification.

INCLUDE only if the email is one of:
- A confirmation that THEIR application was received ("thank you for applying", "your application has been received")
- An interview invitation or scheduling email addressed to them specifically
- A rejection notice for an application they submitted
- An offer
- A direct reply in a thread with a real recruiter/hiring manager about their candidacy

EXCLUDE (is_job_related = false) if the email is:
- A job alert, digest, or "new jobs matching your search" type email (Indeed, LinkedIn, Monster, Dice, ZipRecruiter, Keysight alerts, aggregators, etc.)
- A suggestion of jobs they have NOT applied to ("similar jobs", "new opportunities", "X picked jobs for you")
- Unrelated to job applications (banking, shopping, receipts, newsletters)

Email subject: {email['subject']}
From: {email['from']}
Date: {email['date']}
Snippet: {email['snippet']}

Respond ONLY with valid JSON, no markdown formatting, no code fences, no other text, in this exact format:
{{
  "is_job_related": true or false,
  "company": "company name or null",
  "role": "job title or null",
  "job_id": "requisition/job ID number if mentioned (e.g. REQ2026070436, 26WD94900), otherwise null",
  "location": "city/state or 'Remote' or null if not mentioned",
  "status": "one of: applied, interview_invite, rejected, offer — pick whichever is the most advanced stage implied by this email (e.g. a note about scheduling a call is interview_invite, a generic status-check reply about a still-pending application is applied)",
  "date_applied": "the email date if this looks like the original application confirmation, otherwise null",
  "notes": "brief one-line note, or null"
}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"Failed to parse JSON for: {email['subject']}")
        print(f"  Raw response was: {raw[:200]}")
        return None


def find_location_with_search(company, role, job_id=None):
    def try_search(query_context):
        prompt = f"""Find the SPECIFIC location where this exact job posting is based — NOT the company's headquarters and NOT a generic office location from a company-overview page (e.g. a Wikipedia infobox or LinkedIn's "headquarters" field). Job postings are often based in different cities than HQ, especially internships.

{query_context}

Steps:
1. Search the web for the actual job posting — prefer the company's own careers page, or LinkedIn/Indeed — searching by job ID/requisition number first if one is given, since that finds the exact posting instead of a generic company page.
2. If you find a promising posting URL, fetch it with the web_fetch tool and read the location stated ON that specific posting page — that's the best source. If the fetch fails, is blocked, or the page doesn't clearly state a location, fall back to the location shown in the search results themselves (e.g. Indeed/LinkedIn listing snippets), as long as it looks specific to this posting rather than a generic company-overview page.
3. Only return null if neither the fetched page nor the search results give any location information specific to this job — don't return null just because the fetch step itself failed.
4. Prefer a specific city/office over the company's general headquarters whenever the two differ and you have a specific one available.

Respond ONLY with valid JSON, no markdown, no other text:
{{"location": "City, State" or "Remote" or null if you found no location information for this specific job at all}}"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            tools=[
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 2},
                {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 2, "max_content_tokens": 3000},
            ],
            messages=[{"role": "user", "content": prompt}]
        )
        # With tools in play, Haiku often narrates ("I'll search for...",
        # "Based on the page I fetched...") before or around the JSON answer
        # instead of returning bare JSON, so pull the JSON object out of the
        # response text with a regex instead of assuming the whole text is JSON.
        text_parts = [block.text for block in response.content if block.type == "text"]
        raw = "".join(text_parts)
        matches = re.findall(r'\{[^{}]*"location"[^{}]*\}', raw, re.DOTALL)
        if not matches:
            return None
        try:
            return json.loads(matches[-1]).get("location")
        except json.JSONDecodeError:
            return None

    if job_id:
        loc = try_search(f"Company: {company}\nRole: {role or '(unknown)'}\nJob ID/Requisition Number: {job_id}")
        if loc:
            return loc

    loc = try_search(f"Company: {company}\nRole: {role or '(unknown)'}")
    return loc


def load_location_cache(filename="location_cache.json"):
    if os.path.exists(filename):
        with open(filename) as f:
            return json.load(f)
    return {}


def save_location_cache(cache, filename="location_cache.json"):
    with open(filename, "w") as f:
        json.dump(cache, f, indent=2)


def location_cache_key(company, role):
    return f"{normalize_company(company)}|{normalize_company(role)}"


def normalize_company(name):
    """Lowercase and strip all non-alphanumeric chars so 'Heard Sop',
    'Heardsop', and 'Heard  Sop' all collapse to the same key."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def find_matching_key(norm_name, existing_keys):
    """Fuzzy-match a normalized company name against existing merge keys.
    A prefix match (e.g. 'magna' vs 'magnainternational') is treated as a
    strong signal on its own, since it's how a short-form name relates to
    a full legal name. A mid-string/suffix substring match is weaker, so
    it additionally requires the shorter name to be at least 70% the
    length of the longer one, to avoid false merges."""
    if not norm_name or len(norm_name) < 4:
        return None
    for k in existing_keys:
        if k == norm_name:
            return k
        shorter, longer = sorted([norm_name, k], key=len)
        if len(shorter) < 4:
            continue
        if longer.startswith(shorter):
            return k
        if shorter in longer and len(shorter) / len(longer) >= 0.7:
            return k
    return None


def dedupe_and_merge(records):
    """Merge records primarily by normalized company name — this is the
    most reliable signal since a company's name doesn't change across
    different sending domains. Domain is only used as a last-resort key
    when no company name was extracted at all."""
    status_rank = {"applied": 0, "interview_invite": 1, "offer": 2, "rejected": 2}

    merged = {}
    keys_in_order = []

    for r in records:
        norm_company = normalize_company(r.get("company"))

        if norm_company:
            existing_key = find_matching_key(norm_company, keys_in_order)
            key = existing_key if existing_key else norm_company
        else:
            key = f"nodomain-{r.get('domain') or 'unknown'}"

        if key not in merged:
            merged[key] = r
            keys_in_order.append(key)
        else:
            existing = merged[key]
            if status_rank.get(r["status"], 0) >= status_rank.get(existing["status"], 0):
                existing["status"] = r["status"]
                existing["notes"] = r.get("notes") or existing.get("notes")
            if r.get("date_applied") and not existing.get("date_applied"):
                existing["date_applied"] = r["date_applied"]
            for field in ["location", "role", "job_id"]:
                if not existing.get(field) and r.get(field):
                    existing[field] = r[field]
            if r.get("company") and len(r["company"]) > len(existing.get("company") or ""):
                existing["company"] = r["company"]

    return list(merged.values())


def write_to_excel(records, filename="applications.xlsx"):
    wb = Workbook()
    ws = wb.active
    ws.title = "Applications"

    headers = ["Company", "Role", "Location", "Status", "Date Applied", "Subject", "Notes"]
    ws.append(headers)

    for r in records:
        ws.append([
            r.get("company") or "",
            r.get("role") or "",
            r.get("location") or "",
            r.get("status") or "",
            r.get("date_applied") or "",
            r.get("subject") or "",
            r.get("notes") or "",
        ])

    for col in ws.columns:
        max_len = max((len(str(cell.value)) for cell in col if cell.value), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    wb.save(filename)


def run_extraction_core(
    service,
    target_email_count=200,
    max_location_searches=20,
    applications_json_path="applications.json",
    applications_xlsx_path="applications.xlsx",
    location_cache_path="location_cache.json",
    progress_cb=None,
):
    """Runs the full pipeline (fetch -> classify -> dedupe -> geolocate ->
    write) against whichever Gmail service is passed in. Shared by the
    developer's own CLI run (own account, own files) and the web app's
    per-visitor runs (their account, temp files) — only the scope/paths
    differ between callers. Returns the deduplicated records list.

    location_cache_path defaults to the same shared file for every caller,
    since company+role -> location is public job-posting info, not personal
    data, so different users' runs get to reuse each other's lookups."""

    def progress(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    emails = fetch_candidate_emails(target_count=target_email_count, service=service)

    results = []
    for email in emails:
        email["snippet"] = get_email_body(service, email["id"])
        try:
            extracted = extract_with_claude(email)
        except Exception as e:
            progress(f"  ! Classification failed for '{email['subject']}' ({type(e).__name__}: {e}) — skipping, will retry next run")
            continue

        if extracted and extracted.get("is_job_related"):
            extracted["subject"] = email["subject"]
            extracted["date"] = email["date"]
            extracted["domain"] = email["domain"]
            results.append(extracted)
            progress(f"✓ {extracted['company']} — {extracted['role']} ({extracted['status']})")

            # Incremental save so a mid-run credit/rate-limit/network failure
            # doesn't throw away everything classified so far.
            if applications_json_path:
                partial = [r for r in results if r.get("company") or r.get("role")]
                with open(applications_json_path, "w") as f:
                    json.dump(dedupe_and_merge(partial), f, indent=2)

    results = [r for r in results if r.get("company") or r.get("role")]

    merged = dedupe_and_merge(results)

    # Save immediately after dedup so a crash during location search never
    # loses the extraction pass itself.
    if applications_json_path:
        with open(applications_json_path, "w") as f:
            json.dump(merged, f, indent=2)

    location_cache = load_location_cache(location_cache_path)
    # Only worth spending search credits on applications still active enough
    # to matter — skip rejected/follow-up/etc.
    SEARCH_ELIGIBLE_STATUSES = {"applied", "interview_invite", "offer"}
    status_priority = {"offer": 0, "interview_invite": 1, "applied": 2}

    eligible = [
        r for r in merged
        if not r.get("location") and r.get("company")
        and r.get("status") in SEARCH_ELIGIBLE_STATUSES
    ]
    eligible.sort(key=lambda r: status_priority.get(r.get("status"), 99))

    searches_used = 0
    for r in eligible:
        cache_key = location_cache_key(r["company"], r.get("role"))
        if cache_key in location_cache:
            r["location"] = location_cache[cache_key]
            progress(f"  (cached) {r['company']} — {r.get('role')} → {r['location']}")
        else:
            if searches_used >= max_location_searches:
                progress(f"  (cap reached, {max_location_searches} searches used) skipping {r['company']} — {r.get('role')}")
                continue

            progress(f"Searching for location: {r['company']} — {r.get('role')}")
            searches_used += 1
            try:
                loc = find_location_with_search(r["company"], r.get("role"), r.get("job_id"))
            except Exception as e:
                progress(f"  ! Location search failed for {r['company']} ({type(e).__name__}: {e}) — skipping, will retry next run")
                loc = None

            if loc:
                r["location"] = loc
                location_cache[cache_key] = loc
                save_location_cache(location_cache, location_cache_path)
                progress(f"  → {loc}")

        # Incremental save so progress survives a crash/interrupt mid-loop.
        if applications_json_path:
            with open(applications_json_path, "w") as f:
                json.dump(merged, f, indent=2)

    if applications_xlsx_path:
        write_to_excel(merged, applications_xlsx_path)

    progress(f"\nExtracted {len(results)} raw records, merged to {len(merged)} unique applications")
    return merged


def run_extraction():
    service = get_gmail_service()
    run_extraction_core(service, target_email_count=200, max_location_searches=20)
    print("→ applications.json and applications.xlsx")


if __name__ == "__main__":
    run_extraction()