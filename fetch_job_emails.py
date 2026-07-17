import re
from auth_test import get_gmail_service

QUERY = (
    '(application OR applied OR "thank you for applying" OR interview '
    'OR "your application" OR recruiter OR "next steps" OR offer OR '
    'rejection OR "not moving forward" OR "unfortunately") '
    '-category:promotions -category:social '
    '-from:indeed.com -from:glassdoor.com -from:lensa.com -from:bebee.com '
    '-from:jobright.ai -from:refer.io -from:huntington.com '
    '-from:americanexpress.com -from:nytimes.com -from:teksystems.com'
)


def extract_domain(sender):
    match = re.search(r'@([\w.-]+)', sender)
    return match.group(1).lower() if match else "unknown"


def fetch_candidate_emails(target_count=200):
    """Pulls candidate emails using pagination until target_count raw
    candidates are gathered (or Gmail runs out of matches)."""
    service = get_gmail_service()

    email_data = []
    page_token = None

    while len(email_data) < target_count:
        request_args = {"userId": "me", "q": QUERY, "maxResults": 100}
        if page_token:
            request_args["pageToken"] = page_token

        results = service.users().messages().list(**request_args).execute()
        messages = results.get("messages", [])

        if not messages:
            break

        for msg in messages:
            msg_data = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()

            headers = msg_data["payload"]["headers"]
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(no subject)")
            sender = next((h["value"] for h in headers if h["name"] == "From"), "(unknown)")
            date = next((h["value"] for h in headers if h["name"] == "Date"), "(unknown)")

            email_data.append({
                "id": msg["id"],
                "subject": subject,
                "from": sender,
                "date": date,
                "domain": extract_domain(sender),
            })

        page_token = results.get("nextPageToken")
        print(f"Fetched {len(email_data)} candidates so far...")

        if not page_token:
            break

    print(f"\nFound {len(email_data)} total candidate emails.\n")
    return email_data


if __name__ == "__main__":
    fetch_candidate_emails()