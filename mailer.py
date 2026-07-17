import base64
from email.message import EmailMessage

from auth_test import get_gmail_service


def send_spreadsheet(recipient_email, xlsx_path="applications.xlsx"):
    """Emails the job-application tracker spreadsheet to recipient_email
    as an attachment, sent from the authenticated Gmail account."""
    service = get_gmail_service()

    message = EmailMessage()
    message["To"] = recipient_email
    message["Subject"] = "Your Job Application Tracker spreadsheet"
    message.set_content(
        "Hi,\n\n"
        "Attached is your latest job application tracker spreadsheet.\n\n"
        "— Job Application Tracker"
    )

    with open(xlsx_path, "rb") as f:
        xlsx_data = f.read()

    message.add_attachment(
        xlsx_data,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="applications.xlsx",
    )

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
