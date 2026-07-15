import json
import os
import re
from dotenv import load_dotenv
from anthropic import Anthropic
from openpyxl import Workbook
from fetch_job_emails import fetch_candidate_emails
from auth_test import get_gmail_service

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
  "location": "city/state or 'Remote' or null if not mentioned",
  "status": "one of: applied, interview_invite, rejected, offer, follow_up, other",
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


def find_location_with_search(company, role):
    prompt = f"""Search the web to find the city/state (or "Remote") where this internship/job is based.

Company: {company}
Role: {role if role else "(unknown role)"}

Respond ONLY with valid JSON, no markdown, no other text:
{{"location": "City, State" or "Remote" or null if you cannot determine it with reasonable confidence}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    )

    text_parts = [block.text for block in response.content if block.type == "text"]
    raw = "".join(text_parts).strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        data = json.loads(raw)
        return data.get("location")
    except (json.JSONDecodeError, IndexError):
        return None


PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"}

SHARED_ATS_DOMAINS = {
    "myworkday.com", "greenhouse-mail.io", "hire.lever.co", "icims.com",
    "talent.icims.com", "smartrecruiters.com", "wayup.com", "jobvite.com",
    "linkedin.com", "ycombinator.com", "workatastartup.com"
}


def normalize_company(name):
    """Lowercase and strip all non-alphanumeric chars so 'Heard Sop',
    'Heardsop', and 'Heard  Sop' all collapse to the same key."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def find_matching_key(norm_name, existing_keys):
    """Fuzzy-match a normalized company name against existing merge keys —
    treats substring containment as a match (e.g. 'heard' vs 'heardsop')."""
    if not norm_name:
        return None
    for k in existing_keys:
        if k == norm_name:
            return k
        if len(norm_name) >= 4 and len(k) >= 4 and (norm_name in k or k in norm_name):
            return k
    return None


def dedupe_and_merge(records):
    status_rank = {"applied": 0, "follow_up": 1, "interview_invite": 2,
                   "offer": 3, "rejected": 3, "other": 0}

    merged = {}       # key -> record
    company_keys = {}  # normalized company name -> key actually used in merged

    for r in records:
        domain = r.get("domain") or ""
        norm_company = normalize_company(r.get("company"))

        if domain in PERSONAL_DOMAINS or domain in SHARED_ATS_DOMAINS:
            # Shared/untrustworthy domain — match on normalized company name instead
            existing_key = find_matching_key(norm_company, company_keys.keys())
            if existing_key:
                key = company_keys[existing_key]
            elif norm_company:
                key = norm_company
                company_keys[norm_company] = key
            else:
                key = f"unmatched-{id(r)}"
        else:
            key = domain
            if norm_company:
                company_keys[norm_company] = key

        if key not in merged:
            merged[key] = r
        else:
            existing = merged[key]
            if status_rank.get(r["status"], 0) >= status_rank.get(existing["status"], 0):
                existing["status"] = r["status"]
                existing["notes"] = r.get("notes") or existing.get("notes")
            if r.get("date_applied") and not existing.get("date_applied"):
                existing["date_applied"] = r["date_applied"]
            for field in ["location", "role"]:
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


def run_extraction():
    service = get_gmail_service()
    emails = fetch_candidate_emails(max_results=50)

    results = []
    for email in emails:
        email["snippet"] = get_email_body(service, email["id"])
        extracted = extract_with_claude(email)

        if extracted and extracted.get("is_job_related"):
            extracted["subject"] = email["subject"]
            extracted["date"] = email["date"]
            extracted["domain"] = email["domain"]
            results.append(extracted)
            print(f"✓ {extracted['company']} — {extracted['role']} ({extracted['status']})")

    results = [r for r in results if r.get("company") or r.get("role")]

    merged = dedupe_and_merge(results)

    # Fill in missing locations via web search
    for r in merged:
        if not r.get("location") and r.get("company"):
            print(f"Searching for location: {r['company']} — {r.get('role')}")
            loc = find_location_with_search(r["company"], r.get("role"))
            if loc:
                r["location"] = loc
                print(f"  → {loc}")

    with open("applications.json", "w") as f:
        json.dump(merged, f, indent=2)

    write_to_excel(merged)

    print(f"\nExtracted {len(results)} raw records, merged to {len(merged)} unique applications")
    print("→ applications.json and applications.xlsx")


if __name__ == "__main__":
    run_extraction()