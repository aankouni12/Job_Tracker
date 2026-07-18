import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# readonly — for scanning the inbox; send — for emailing the spreadsheet out
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def build_service_from_credentials(creds_info):
    """Builds a Gmail service for one visitor's own OAuth grant (the web
    sign-in flow), as opposed to get_gmail_service() below which is the
    single shared token.json used by the developer's own CLI scripts."""
    creds = Credentials(**creds_info)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def get_gmail_service():
    creds = None

    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    scopes_ok = creds and set(SCOPES).issubset(set(creds.scopes or []))

    if not creds or not creds.valid or not scopes_ok:
        if creds and creds.expired and creds.refresh_token and scopes_ok:
            creds.refresh(Request())
        else:
            # Either no token yet, or the saved token doesn't cover a scope
            # we now need (e.g. it predates the send permission) — get fresh
            # consent covering everything in SCOPES.
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def test_connection():
    service = get_gmail_service()

    results = service.users().messages().list(
        userId="me", maxResults=5
    ).execute()

    messages = results.get("messages", [])

    if not messages:
        print("No messages found.")
        return

    print(f"Found {len(messages)} messages. Fetching subjects:\n")

    for msg in messages:
        msg_data = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"]
        ).execute()

        headers = msg_data["payload"]["headers"]
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(no subject)")
        sender = next((h["value"] for h in headers if h["name"] == "From"), "(unknown)")

        print(f"- {subject}  |  from: {sender}")


if __name__ == "__main__":
    test_connection()