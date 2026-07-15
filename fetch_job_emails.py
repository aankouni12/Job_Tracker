import re
from auth_test import get_gmail_service

# Gmail search query — narrows down to likely job-application emails
# before we spend LLM calls classifying them
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


def fetch_candidate_emails(max_results=50):
    service = get_gmail_service()

    results = service.users().messages().list(
        userId="me", q=QUERY, maxResults=max_results
    ).execute()

    messages = results.get("messages", [])
    print(f"Found {len(messages)} candidate emails.\n")

    email_data = []

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

        print(f"- {subject}  |  from: {sender}")

    return email_data


if __name__ == "__main__":
    fetch_candidate_emails()