import os
import json
import uuid
import logging
import smtplib
import asyncio

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any, Awaitable, Callable

from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, firestore

from livekit.agents import (
    Agent,
    AgentSession,
    ConversationItemAddedEvent,
    JobContext,
    UserInputTranscribedEvent,
    WorkerOptions,
    cli,
    function_tool,
)

from livekit.plugins import google


# =========================================================
# 1. LOAD CONFIGURATION
# =========================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(
    "customer-support-agent"
)

logger.info(
    "AGENT BUILD: FIREBASE-TICKET-EMAIL-V3"
)


# =========================================================
# 2. REQUIRED ENVIRONMENT VARIABLES
# =========================================================

REQUIRED_ENVIRONMENT_VARIABLES = [
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "GOOGLE_API_KEY",
    "FIREBASE_CREDENTIALS_JSON",
]

missing_environment_variables = [
    variable_name
    for variable_name
    in REQUIRED_ENVIRONMENT_VARIABLES
    if not os.getenv(variable_name)
]

if missing_environment_variables:
    raise RuntimeError(
        "Missing required environment variables: "
        + ", ".join(
            missing_environment_variables
        )
    )


GOOGLE_API_KEY = os.environ[
    "GOOGLE_API_KEY"
]

FIREBASE_CREDENTIALS_JSON = os.environ[
    "FIREBASE_CREDENTIALS_JSON"
]


# =========================================================
# 3. GEMINI LIVE CONFIGURATION
# =========================================================

GEMINI_LIVE_MODEL = os.getenv(
    "GEMINI_LIVE_MODEL",
    "gemini-2.5-flash-native-audio-preview-12-2025",
)

# Flare is not a supported Gemini Live voice.
# Zephyr is a valid bright voice.
GEMINI_VOICE = os.getenv(
    "GEMINI_VOICE",
    "Zephyr",
)


# =========================================================
# 4. EMAIL CONFIGURATION
# =========================================================

SMTP_SENDER_EMAIL = os.getenv(
    "SMTP_SENDER_EMAIL",
    "",
).strip()

SMTP_APP_PASSWORD = (
    os.getenv(
        "SMTP_APP_PASSWORD",
        "",
    )
    .replace(" ", "")
    .strip()
)


# =========================================================
# 5. CUSTOMER-SERVICE CONFIGURATION
# =========================================================

SUPPORTED_LANGUAGES = (
    "English, Telugu, Hindi, Tamil, Kannada, "
    "Malayalam, and Odia"
)

WELCOME_MESSAGE = (
    "Welcome to customer support. "
    "You can speak in English, Telugu, Hindi, Tamil, "
    "Kannada, Malayalam, or Odia. "
    "Which language would you like to use?"
)


# =========================================================
# 6. FIREBASE INITIALIZATION
# =========================================================

def initialize_firebase() -> None:
    """
    Initialize Firebase Admin only once in each worker process.
    """

    if firebase_admin._apps:
        logger.info(
            "Firebase is already initialized."
        )
        return

    try:
        service_account_data = json.loads(
            FIREBASE_CREDENTIALS_JSON
        )

        firebase_project_id = service_account_data.get(
            "project_id",
            "",
        )

        if not firebase_project_id:
            raise RuntimeError(
                "Firebase service-account JSON does not "
                "contain a project_id."
            )

        firebase_credential = credentials.Certificate(
            service_account_data
        )

        firebase_admin.initialize_app(
            firebase_credential
        )

        logger.info(
            "Firebase initialized successfully. Project: %s",
            firebase_project_id,
        )

    except json.JSONDecodeError as error:
        logger.exception(
            "FIREBASE_CREDENTIALS_JSON is invalid JSON."
        )

        raise RuntimeError(
            "FIREBASE_CREDENTIALS_JSON is invalid JSON."
        ) from error

    except Exception:
        logger.exception(
            "Firebase initialization failed."
        )
        raise


initialize_firebase()

database = firestore.client()


# =========================================================
# 7. GENERAL HELPERS
# =========================================================

def clean_text(
    value: Any,
) -> str:
    """
    Convert a value to cleaned text.
    """

    if value is None:
        return ""

    return str(value).strip()


def normalize_email(
    value: Any,
) -> str:
    """
    Normalize email addresses for storage and comparison.
    """

    return clean_text(
        value
    ).lower()


def normalize_order_id(
    value: Any,
) -> str:
    """
    Normalize an order ID.
    """

    return clean_text(
        value
    ).upper()


def normalize_ticket_id(
    value: Any,
) -> str:
    """
    Normalize a ticket ID.
    """

    return clean_text(
        value
    ).upper()


def create_ticket_id() -> str:
    """
    Create a readable ticket ID.

    Example:
        TICK-A12BC34D
    """

    random_part = (
        uuid.uuid4()
        .hex[:8]
        .upper()
    )

    return f"TICK-{random_part}"


def validate_email(
    email: str,
) -> bool:
    """
    Perform basic email validation.
    """

    email = normalize_email(
        email
    )

    if not email:
        return False

    if "@" not in email:
        return False

    local_part, domain = email.rsplit(
        "@",
        1,
    )

    if not local_part:
        return False

    if "." not in domain:
        return False

    return True


# =========================================================
# 8. FIRESTORE CREATE TICKET
# =========================================================

def create_ticket_in_firestore(
    customer_name: str,
    email: str,
    issue: str,
    order_id: str = "",
    priority: str = "NORMAL",
    status: str = "Open",
) -> dict:
    """
    Create and verify a support ticket in Firestore.
    """

    customer_name = clean_text(
        customer_name
    )

    email = normalize_email(
        email
    )

    issue = clean_text(
        issue
    )

    order_id = normalize_order_id(
        order_id
    )

    priority = (
        clean_text(priority).upper()
        or "NORMAL"
    )

    status = (
        clean_text(status)
        or "Open"
    )

    if not customer_name:
        return {
            "ok": False,
            "message": (
                "Customer name is required."
            ),
        }

    if not email:
        return {
            "ok": False,
            "message": (
                "Customer email is required."
            ),
        }

    if not validate_email(email):
        return {
            "ok": False,
            "message": (
                "The customer email address is invalid."
            ),
        }

    if not issue:
        return {
            "ok": False,
            "message": (
                "Issue description is required."
            ),
        }

    ticket_id = create_ticket_id()

    ticket_data = {
        "ticket_id": ticket_id,
        "customer_name": customer_name,
        "customer_name_lower": (
            customer_name.lower()
        ),
        "email": email,
        "issue": issue,
        "order_id": order_id,
        "status": status,
        "priority": priority,
        "created_at": (
            firestore.SERVER_TIMESTAMP
        ),
        "updated_at": (
            firestore.SERVER_TIMESTAMP
        ),
    }

    try:
        logger.info(
            "Writing ticket %s to Firestore.",
            ticket_id,
        )

        ticket_reference = (
            database.collection("tickets")
            .document(ticket_id)
        )

        write_result = ticket_reference.set(
            ticket_data
        )

        logger.info(
            "Firestore write completed for %s: %s",
            ticket_id,
            write_result,
        )

        saved_document = (
            ticket_reference.get()
        )

        if not saved_document.exists:
            raise RuntimeError(
                "Firestore write verification failed."
            )

        saved_data = (
            saved_document.to_dict()
            or {}
        )

        if (
            saved_data.get("ticket_id")
            != ticket_id
        ):
            raise RuntimeError(
                "The saved ticket ID did not match."
            )

        logger.info(
            "Ticket saved and verified successfully: %s",
            ticket_id,
        )

        return {
            "ok": True,
            "ticket_id": ticket_id,
            "customer_name": customer_name,
            "email": email,
            "issue": issue,
            "order_id": order_id,
            "status": status,
            "priority": priority,
        }

    except Exception as error:
        logger.exception(
            "Firestore ticket creation failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be saved "
                "in Firestore."
            ),
            "error": str(error),
        }


# =========================================================
# 9. FIRESTORE RETRIEVE TICKET
# =========================================================

def get_ticket_from_firestore(
    ticket_id: str,
) -> dict:
    """
    Retrieve one ticket by exact ticket ID.
    """

    ticket_id = normalize_ticket_id(
        ticket_id
    )

    if not ticket_id:
        return {
            "ok": False,
            "message": "Ticket ID is required.",
        }

    try:
        document = (
            database.collection("tickets")
            .document(ticket_id)
            .get()
        )

        if not document.exists:
            return {
                "ok": False,
                "message": (
                    f"Ticket {ticket_id} was not found."
                ),
            }

        data = document.to_dict() or {}

        return {
            "ok": True,
            "ticket": {
                "ticket_id": document.id,
                "customer_name": data.get(
                    "customer_name",
                    "Customer",
                ),
                "issue": data.get(
                    "issue",
                    "No description",
                ),
                "order_id": data.get(
                    "order_id",
                    "",
                ),
                "status": data.get(
                    "status",
                    "Open",
                ),
                "priority": data.get(
                    "priority",
                    "NORMAL",
                ),
                "email": data.get(
                    "email",
                    "",
                ),
            },
        }

    except Exception as error:
        logger.exception(
            "Firestore ticket retrieval failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be retrieved."
            ),
            "error": str(error),
        }


# =========================================================
# 10. FIRESTORE FIND TICKETS
# =========================================================

def find_tickets_in_firestore(
    email: str,
) -> dict:
    """
    Find the customer's most recent tickets by email.
    """

    email = normalize_email(
        email
    )

    if not validate_email(email):
        return {
            "ok": False,
            "message": (
                "A valid customer email is required."
            ),
            "tickets": [],
        }

    try:
        documents = (
            database.collection("tickets")
            .where(
                "email",
                "==",
                email,
            )
            .limit(10)
            .stream()
        )

        tickets = []

        for document in documents:
            data = (
                document.to_dict()
                or {}
            )

            tickets.append({
                "ticket_id": document.id,
                "issue": data.get(
                    "issue",
                    "No description",
                ),
                "status": data.get(
                    "status",
                    "Open",
                ),
                "priority": data.get(
                    "priority",
                    "NORMAL",
                ),
            })

        return {
            "ok": True,
            "tickets": tickets,
        }

    except Exception as error:
        logger.exception(
            "Firestore ticket search failed."
        )

        return {
            "ok": False,
            "message": (
                "Tickets could not be searched."
            ),
            "tickets": [],
            "error": str(error),
        }


# =========================================================
# 11. FIRESTORE UPDATE TICKET
# =========================================================

def update_ticket_in_firestore(
    ticket_id: str,
    verification_email: str,
    new_issue: str = "",
    new_status: str = "",
) -> dict:
    """
    Update a ticket after registered-email verification.
    """

    ticket_id = normalize_ticket_id(
        ticket_id
    )

    verification_email = normalize_email(
        verification_email
    )

    existing_result = (
        get_ticket_from_firestore(
            ticket_id
        )
    )

    if not existing_result.get("ok"):
        return existing_result

    existing_ticket = (
        existing_result["ticket"]
    )

    saved_email = normalize_email(
        existing_ticket.get("email")
    )

    if (
        not saved_email
        or saved_email != verification_email
    ):
        return {
            "ok": False,
            "message": (
                "Email verification failed. "
                "The ticket was not updated."
            ),
        }

    changes = {
        "updated_at": (
            firestore.SERVER_TIMESTAMP
        ),
    }

    new_issue = clean_text(
        new_issue
    )

    new_status = clean_text(
        new_status
    )

    if new_issue:
        changes["issue"] = new_issue

    if new_status:
        allowed_statuses = {
            "open": "Open",
            "in progress": "In Progress",
            "in-progress": "In Progress",
            "resolved": "Resolved",
            "closed": "Closed",
        }

        normalized_status = (
            new_status.lower()
        )

        if (
            normalized_status
            not in allowed_statuses
        ):
            return {
                "ok": False,
                "message": (
                    "Status must be Open, "
                    "In Progress, Resolved, or Closed."
                ),
            }

        changes["status"] = (
            allowed_statuses[
                normalized_status
            ]
        )

    if len(changes) == 1:
        return {
            "ok": False,
            "message": (
                "Provide a new issue or new status."
            ),
        }

    try:
        database.collection("tickets").document(
            ticket_id
        ).update(changes)

        logger.info(
            "Ticket updated successfully: %s",
            ticket_id,
        )

        return {
            "ok": True,
            "ticket_id": ticket_id,
            "message": (
                f"Ticket {ticket_id} was updated."
            ),
        }

    except Exception as error:
        logger.exception(
            "Firestore ticket update failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be updated."
            ),
            "error": str(error),
        }


# =========================================================
# 12. FIRESTORE DELETE TICKET
# =========================================================

def delete_ticket_from_firestore(
    ticket_id: str,
    verification_email: str,
) -> dict:
    """
    Delete a ticket after registered-email verification.
    """

    ticket_id = normalize_ticket_id(
        ticket_id
    )

    verification_email = normalize_email(
        verification_email
    )

    existing_result = (
        get_ticket_from_firestore(
            ticket_id
        )
    )

    if not existing_result.get("ok"):
        return existing_result

    existing_ticket = (
        existing_result["ticket"]
    )

    saved_email = normalize_email(
        existing_ticket.get("email")
    )

    if (
        not saved_email
        or saved_email != verification_email
    ):
        return {
            "ok": False,
            "message": (
                "Email verification failed. "
                "The ticket was not deleted."
            ),
        }

    try:
        database.collection("tickets").document(
            ticket_id
        ).delete()

        logger.info(
            "Ticket deleted successfully: %s",
            ticket_id,
        )

        return {
            "ok": True,
            "ticket_id": ticket_id,
            "message": (
                f"Ticket {ticket_id} was deleted."
            ),
        }

    except Exception as error:
        logger.exception(
            "Firestore ticket deletion failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be deleted."
            ),
            "error": str(error),
        }


# =========================================================
# 13. SEND TICKET EMAIL
# =========================================================

def send_ticket_email(
    customer_name: str,
    recipient_email: str,
    ticket_id: str,
    issue: str,
    status: str,
    order_id: str = "",
) -> dict:
    """
    Send a confirmation email after Firestore succeeds.
    """

    customer_name = (
        clean_text(customer_name)
        or "Customer"
    )

    recipient_email = normalize_email(
        recipient_email
    )

    ticket_id = normalize_ticket_id(
        ticket_id
    )

    issue = clean_text(
        issue
    )

    status = clean_text(
        status
    )

    order_id = normalize_order_id(
        order_id
    )

    if not SMTP_SENDER_EMAIL:
        logger.warning(
            "SMTP_SENDER_EMAIL is missing."
        )

        return {
            "ok": False,
            "message": (
                "Sender email is not configured."
            ),
        }

    if not SMTP_APP_PASSWORD:
        logger.warning(
            "SMTP_APP_PASSWORD is missing."
        )

        return {
            "ok": False,
            "message": (
                "Gmail App Password is not configured."
            ),
        }

    if not validate_email(
        recipient_email
    ):
        return {
            "ok": False,
            "message": (
                "The recipient email is invalid."
            ),
        }

    safe_customer_name = escape(
        customer_name
    )

    safe_ticket_id = escape(
        ticket_id
    )

    safe_issue = escape(
        issue
    )

    safe_status = escape(
        status
    )

    safe_order_id = escape(
        order_id
    )

    plain_order_line = ""

    if order_id:
        plain_order_line = (
            f"Order ID: {order_id}\n"
        )

    plain_body = (
        f"Dear {customer_name},\n\n"
        "Your support ticket has been created successfully.\n\n"
        f"Ticket ID: {ticket_id}\n"
        f"{plain_order_line}"
        f"Issue: {issue}\n"
        f"Status: {status}\n\n"
        "Please keep this ticket ID for future reference.\n\n"
        "Regards,\n"
        "Customer Support Team"
    )

    html_order_row = ""

    if safe_order_id:
        html_order_row = f"""
        <tr>
          <td style="padding:8px;font-weight:bold;">
            Order ID
          </td>
          <td style="padding:8px;">
            {safe_order_id}
          </td>
        </tr>
        """

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <body style="
        margin:0;
        padding:20px;
        background:#f1f5f9;
        font-family:Arial,sans-serif;
        color:#0f172a;
    ">
      <div style="
          max-width:600px;
          margin:auto;
          padding:24px;
          background:#ffffff;
          border-radius:12px;
          box-shadow:0 4px 15px rgba(0,0,0,0.08);
      ">
        <h2 style="
            margin-top:0;
            color:#2563eb;
        ">
          Support Ticket Created
        </h2>

        <p>
          Dear {safe_customer_name},
        </p>

        <p>
          Your support ticket has been created successfully.
        </p>

        <table style="
            width:100%;
            border-collapse:collapse;
            background:#f8fafc;
        ">
          <tr>
            <td style="padding:8px;font-weight:bold;">
              Ticket ID
            </td>
            <td style="padding:8px;">
              {safe_ticket_id}
            </td>
          </tr>

          {html_order_row}

          <tr>
            <td style="padding:8px;font-weight:bold;">
              Issue
            </td>
            <td style="padding:8px;">
              {safe_issue}
            </td>
          </tr>

          <tr>
            <td style="padding:8px;font-weight:bold;">
              Status
            </td>
            <td style="padding:8px;">
              {safe_status}
            </td>
          </tr>
        </table>

        <p>
          Please keep the ticket ID for future reference.
        </p>

        <p>
          Regards,<br>
          Customer Support Team
        </p>
      </div>
    </body>
    </html>
    """

    try:
        message = MIMEMultipart(
            "alternative"
        )

        message["From"] = (
            f"Customer Support "
            f"<{SMTP_SENDER_EMAIL}>"
        )

        message["To"] = recipient_email

        message["Subject"] = (
            f"Support Ticket Created: {ticket_id}"
        )

        message.attach(
            MIMEText(
                plain_body,
                "plain",
                "utf-8",
            )
        )

        message.attach(
            MIMEText(
                html_body,
                "html",
                "utf-8",
            )
        )

        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
            timeout=15,
        ) as smtp_server:
            smtp_server.login(
                SMTP_SENDER_EMAIL,
                SMTP_APP_PASSWORD,
            )

            smtp_server.send_message(
                message
            )

        logger.info(
            "Ticket email sent successfully for %s.",
            ticket_id,
        )

        return {
            "ok": True,
            "message": (
                "Confirmation email was sent."
            ),
        }

    except Exception as error:
        logger.exception(
            "Ticket email delivery failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket was created, but "
                "the email could not be sent."
            ),
            "error": str(error),
        }


# =========================================================
# 14. CUSTOMER SUPPORT AGENT
# =========================================================

class CustomerSupportAgent(Agent):
    """
    Customer-service agent with real Firestore tools.
    """

    def __init__(
        self,
        customer_details: dict,
        send_browser_event: Callable[
            [dict],
            Awaitable[None],
        ],
    ) -> None:
        self.customer_details = (
            customer_details
        )

        self.send_browser_event = (
            send_browser_event
        )

        super().__init__(
            instructions=(
                "You are a fast, professional customer-service "
                "voice assistant. "

                f"The supported languages are "
                f"{SUPPORTED_LANGUAGES}. "

                "At the start, greet the customer and ask which "
                "supported language the customer wants to use. "

                "After the customer chooses a language, continue "
                "in that language. "

                "Keep responses short. Use no more than one or "
                "two short sentences. "

                "The website may already contain the customer's "
                "name, email, and order ID. "

                "Before asking for customer name, email, or order "
                "ID, call get_submitted_details. "

                "Do not repeatedly ask for information that was "
                "already submitted. "

                "When the customer asks to create or raise a "
                "support ticket, collect the issue description. "

                "Once the issue is known, call "
                "create_support_ticket immediately. "

                "Never invent a ticket ID. "

                "Never claim a ticket was created unless the "
                "create_support_ticket tool confirms that the "
                "Firestore write succeeded. "

                "After successful ticket creation, clearly state "
                "the ticket ID and whether the confirmation email "
                "was sent. "

                "For ticket retrieval, call get_support_ticket. "

                "For ticket search, call find_support_tickets. "

                "For ticket updates, call update_support_ticket. "

                "Before permanent deletion, get explicit customer "
                "confirmation and then call delete_support_ticket."
            )
        )


    # =====================================================
    # TOOL: GET SUBMITTED DETAILS
    # =====================================================

    @function_tool
    async def get_submitted_details(
        self,
    ) -> str:
        """
        Retrieve customer details submitted through the website.
        """

        customer_name = clean_text(
            self.customer_details.get(
                "customer_name"
            )
        )

        email = normalize_email(
            self.customer_details.get(
                "email"
            )
        )

        order_id = normalize_order_id(
            self.customer_details.get(
                "order_id"
            )
        )

        details = []

        if customer_name:
            details.append(
                f"customer name is {customer_name}"
            )

        if email:
            details.append(
                "the registered email was received securely"
            )

        if order_id:
            details.append(
                f"order ID is {order_id}"
            )

        if not details:
            return (
                "No details were submitted through the website."
            )

        return (
            "The website has already provided these details: "
            + "; ".join(details)
            + ". Do not ask for these values again."
        )


    # =====================================================
    # TOOL: CREATE TICKET
    # =====================================================

    @function_tool
    @function_tool
async def create_support_ticket(
    self,
    issue: str,
    customer_name: str = "",
    email: str = "",
    order_id: str = "",
) -> str:
    """
    Create and store a support ticket in Firestore.

    Website-submitted details take priority over values
    interpreted from speech.

    Args:
        issue: Description of the customer's problem.
        customer_name: Customer name if spoken.
        email: Customer email if spoken.
        order_id: Related order ID if available.
    """

    submitted_customer_name = clean_text(
        self.customer_details.get(
            "customer_name"
        )
    )

    submitted_email = normalize_email(
        self.customer_details.get(
            "email"
        )
    )

    submitted_order_id = normalize_order_id(
        self.customer_details.get(
            "order_id"
        )
    )

    spoken_customer_name = clean_text(
        customer_name
    )

    spoken_email = normalize_email(
        email
    )

    spoken_order_id = normalize_order_id(
        order_id
    )

    # Website values take priority because typed values
    # are more reliable than voice transcription.
    final_customer_name = (
        submitted_customer_name
        or spoken_customer_name
    )

    final_email = (
        submitted_email
        or spoken_email
    )

    final_order_id = (
        submitted_order_id
        or spoken_order_id
    )

    final_issue = clean_text(
        issue
    )

    logger.info(
        "Preparing ticket. "
        "Submitted name available: %s. "
        "Submitted email available: %s. "
        "Submitted order available: %s.",
        bool(submitted_customer_name),
        bool(submitted_email),
        bool(submitted_order_id),
    )

    if not final_customer_name:
        return (
            "Ticket was not created. "
            "Please provide only your name."
        )

    if not final_email:
        return (
            "Ticket was not created. "
            "Please type your email in the website form "
            "and press Submit Details to Agent."
        )

    if not validate_email(
        final_email
    ):
        logger.warning(
            "Ticket email validation failed. "
            "Email source: %s.",
            (
                "website"
                if submitted_email
                else "speech"
            ),
        )

        return (
            "Ticket was not created because the submitted "
            "email format is invalid. Please type the email "
            "in the website form and submit it again."
        )

    if not final_issue:
        return (
            "Ticket was not created. "
            "Please describe the issue."
        )

    database_result = await asyncio.to_thread(
        create_ticket_in_firestore,
        final_customer_name,
        final_email,
        final_issue,
        final_order_id,
        "NORMAL",
        "Open",
    )

    if not database_result.get(
        "ok"
    ):
        logger.error(
            "Ticket database operation failed: %s",
            database_result.get(
                "error",
                database_result.get(
                    "message",
                    "Unknown error",
                ),
            ),
        )

        await self.send_browser_event({
            "type": "status",
            "message": (
                "Ticket creation failed."
            ),
        })

        return (
            "The ticket could not be saved. "
            "Please try again."
        )

    ticket_id = database_result[
        "ticket_id"
    ]

    self.customer_details.update({
        "customer_name": (
            final_customer_name
        ),
        "email": final_email,
        "order_id": final_order_id,
        "last_ticket_id": ticket_id,
        "last_issue": final_issue,
    })

    await self.send_browser_event({
        "type": "ticket_created",
        "ticket_id": ticket_id,
        "status": "Open",
    })

    await self.send_browser_event({
        "type": "transcript",
        "sender": "System",
        "text": (
            f"Ticket {ticket_id} was saved "
            "successfully."
        ),
    })

    email_result = await asyncio.to_thread(
        send_ticket_email,
        final_customer_name,
        final_email,
        ticket_id,
        final_issue,
        "Open",
        final_order_id,
    )

    if email_result.get(
        "ok"
    ):
        await self.send_browser_event({
            "type": "status",
            "message": (
                "Ticket created and email sent."
            ),
        })

        return (
            f"Ticket {ticket_id} was created successfully. "
            "A confirmation email was sent to the "
            "registered email address."
        )

    logger.warning(
        "Ticket %s was saved, but email delivery failed: %s",
        ticket_id,
        email_result.get(
            "error",
            email_result.get(
                "message",
                "Unknown email error",
            ),
        ),
    )

    await self.send_browser_event({
        "type": "status",
        "message": (
            "Ticket created, but email delivery failed."
        ),
    })

    return (
        f"Ticket {ticket_id} was created successfully. "
        "However, the confirmation email could not be sent."
    )


    # =====================================================
    # TOOL: RETRIEVE TICKET
    # =====================================================

    @function_tool
    async def get_support_ticket(
        self,
        ticket_id: str,
    ) -> str:
        """
        Retrieve one ticket using its exact ticket ID.
        """

        result = await asyncio.to_thread(
            get_ticket_from_firestore,
            ticket_id,
        )

        if not result.get("ok"):
            return result.get(
                "message",
                "The ticket could not be retrieved.",
            )

        ticket = result["ticket"]

        return (
            f"Ticket {ticket['ticket_id']} is "
            f"{ticket['status']}. "
            f"The issue is {ticket['issue']}."
        )


    # =====================================================
    # TOOL: FIND CUSTOMER TICKETS
    # =====================================================

    @function_tool
    async def find_support_tickets(
        self,
        email: str = "",
    ) -> str:
        """
        Find customer tickets using registered email.
        """

        final_email = (
            normalize_email(email)
            or normalize_email(
                self.customer_details.get(
                    "email"
                )
            )
        )

        if not final_email:
            return (
                "Ask only for the registered email address."
            )

        result = await asyncio.to_thread(
            find_tickets_in_firestore,
            final_email,
        )

        if not result.get("ok"):
            return result.get(
                "message",
                "Tickets could not be searched.",
            )

        tickets = result.get(
            "tickets",
            [],
        )

        if not tickets:
            return (
                "No support tickets were found under "
                "the registered email."
            )

        descriptions = [
            (
                f"{ticket['ticket_id']} is "
                f"{ticket['status']} for "
                f"{ticket['issue']}"
            )
            for ticket in tickets
        ]

        return "; ".join(
            descriptions
        )


    # =====================================================
    # TOOL: UPDATE TICKET
    # =====================================================

    @function_tool
    async def update_support_ticket(
        self,
        ticket_id: str,
        new_issue: str = "",
        new_status: str = "",
        email: str = "",
    ) -> str:
        """
        Update a ticket issue or status.

        Registered email is required for verification.
        """

        final_email = (
            normalize_email(email)
            or normalize_email(
                self.customer_details.get(
                    "email"
                )
            )
        )

        if not final_email:
            return (
                "Ask only for the registered email address."
            )

        result = await asyncio.to_thread(
            update_ticket_in_firestore,
            ticket_id,
            final_email,
            new_issue,
            new_status,
        )

        return result.get(
            "message",
            "The ticket could not be updated.",
        )


    # =====================================================
    # TOOL: DELETE TICKET
    # =====================================================

    @function_tool
    async def delete_support_ticket(
        self,
        ticket_id: str,
        confirmed: bool,
        email: str = "",
    ) -> str:
        """
        Permanently delete a verified support ticket.

        Args:
            ticket_id: Exact ticket ID.
            confirmed: True only after explicit confirmation.
            email: Registered email if not already submitted.
        """

        if not confirmed:
            return (
                "The ticket was not deleted. "
                "Ask the customer to explicitly confirm "
                "permanent deletion."
            )

        final_email = (
            normalize_email(email)
            or normalize_email(
                self.customer_details.get(
                    "email"
                )
            )
        )

        if not final_email:
            return (
                "Ask only for the registered email address."
            )

        result = await asyncio.to_thread(
            delete_ticket_from_firestore,
            ticket_id,
            final_email,
        )

        if result.get("ok"):
            await self.send_browser_event({
                "type": "status",
                "message": (
                    "Ticket deleted successfully."
                ),
            })

        return result.get(
            "message",
            "The ticket could not be deleted.",
        )


# =========================================================
# 15. LIVEKIT SESSION ENTRY POINT
# =========================================================

async def entrypoint(
    ctx: JobContext,
) -> None:
    """
    Run one customer-support voice session.
    """

    logger.info(
        "Agent job received for room: %s",
        ctx.room.name,
    )

    customer_details = {
        "customer_name": "",
        "email": "",
        "order_id": "",
        "last_ticket_id": "",
        "last_issue": "",
    }

    try:
        await ctx.connect()

        logger.info(
            "Agent connected to room: %s",
            ctx.room.name,
        )

    except Exception:
        logger.exception(
            "Agent could not connect to LiveKit."
        )
        raise


    # =====================================================
    # BROWSER EVENT PUBLISHER
    # =====================================================

    async def send_browser_event(
        event_data: dict,
    ) -> None:
        """
        Send JSON events to index.html.
        """

        try:
            payload = json.dumps(
                event_data,
                ensure_ascii=False,
            ).encode("utf-8")

            await (
                ctx.room
                .local_participant
                .publish_data(
                    payload,
                    reliable=True,
                )
            )

        except Exception:
            logger.exception(
                "Browser event publishing failed."
            )


    # =====================================================
    # CREATE AGENT AND SESSION
    # =====================================================

    support_agent = CustomerSupportAgent(
        customer_details,
        send_browser_event,
    )

    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=GEMINI_LIVE_MODEL,
            api_key=GOOGLE_API_KEY,
            voice=GEMINI_VOICE,
            temperature=0.2,
            instructions=(
                "Respond quickly and naturally. "
                "Use no more than two short sentences. "
                "Use a function tool whenever an operation must "
                "be performed. "
                "Never pretend to create, retrieve, update, or "
                "delete a ticket without calling the relevant "
                "tool. "
                "Never invent a ticket ID."
            ),
        )
    )


    # =====================================================
    # USER TRANSCRIPT
    # =====================================================

    @session.on(
        "user_input_transcribed"
    )
    def on_user_input_transcribed(
        event: UserInputTranscribedEvent,
    ) -> None:
        if not event.is_final:
            return

        transcript = clean_text(
            event.transcript
        )

        if not transcript:
            return

        logger.info(
            "Customer transcript: %s",
            transcript,
        )

        asyncio.create_task(
            send_browser_event({
                "type": "transcript",
                "sender": "User",
                "text": transcript,
            })
        )


    # =====================================================
    # AGENT TRANSCRIPT
    # =====================================================

    @session.on(
        "conversation_item_added"
    )
    def on_conversation_item_added(
        event: ConversationItemAddedEvent,
    ) -> None:
        item = event.item

        role = clean_text(
            getattr(
                item,
                "role",
                "",
            )
        ).lower()

        if role not in {
            "assistant",
            "agent",
        }:
            return

        text = clean_text(
            getattr(
                item,
                "text_content",
                "",
            )
        )

        if not text:
            return

        logger.info(
            "Agent transcript: %s",
            text,
        )

        asyncio.create_task(
            send_browser_event({
                "type": "transcript",
                "sender": "Agent",
                "text": text,
            })
        )


    # =====================================================
    # RECEIVE HTML FORM DATA
    # =====================================================

    @ctx.room.on(
        "data_received"
    )
    def on_data_received(
        data_packet,
    ) -> None:
        async def process_data_packet() -> None:
            try:
                payload = json.loads(
                    data_packet.data.decode(
                        "utf-8"
                    )
                )

                action = clean_text(
                    payload.get("type")
                )

                logger.info(
                    "Browser action received: %s",
                    action,
                )

                if action == "email_submission":
                    submitted_name = clean_text(
                        payload.get(
                            "customer_name"
                        )
                    )

                    submitted_email = normalize_email(
                        payload.get(
                            "email"
                        )
                    )

                    submitted_order_id = (
                        normalize_order_id(
                            payload.get(
                                "order_id"
                            )
                        )
                    )

                    if submitted_name:
                        customer_details[
                            "customer_name"
                        ] = submitted_name

                    if submitted_email:
                        customer_details[
                            "email"
                        ] = submitted_email

                    if submitted_order_id:
                        customer_details[
                            "order_id"
                        ] = submitted_order_id

                    logger.info(
                        "Customer details updated. "
                        "Name present: %s. "
                        "Email present: %s. "
                        "Order present: %s.",
                        bool(
                            customer_details[
                                "customer_name"
                            ]
                        ),
                        bool(
                            customer_details[
                                "email"
                            ]
                        ),
                        bool(
                            customer_details[
                                "order_id"
                            ]
                        ),
                    )

                    await send_browser_event({
                        "type": "status",
                        "message": (
                            "Customer details received."
                        ),
                    })

                    await send_browser_event({
                        "type": "transcript",
                        "sender": "System",
                        "text": (
                            "Your submitted customer details "
                            "were received securely."
                        ),
                    })

                    await session.generate_reply(
                        instructions=(
                            "The customer submitted details "
                            "through the website. "
                            "Call get_submitted_details now. "
                            "Confirm receipt in one short sentence. "
                            "Do not ask again for any detail that "
                            "was already submitted."
                        )
                    )

                elif action == "manual_escalate":
                    escalation_issue = (
                        clean_text(
                            payload.get(
                                "reason"
                            )
                        )
                        or customer_details.get(
                            "last_issue"
                        )
                        or (
                            "Customer requested "
                            "manual escalation"
                        )
                    )

                    customer_details[
                        "last_issue"
                    ] = escalation_issue

                    await send_browser_event({
                        "type": "transcript",
                        "sender": "System",
                        "text": (
                            "Your escalation request "
                            "was received."
                        ),
                    })

                    await session.generate_reply(
                        instructions=(
                            "The customer requested escalation. "
                            "Briefly confirm that the request "
                            "was received."
                        )
                    )

            except Exception:
                logger.exception(
                    "Browser data processing failed."
                )

        asyncio.create_task(
            process_data_packet()
        )


    # =====================================================
    # START REALTIME SESSION
    # =====================================================

    try:
        logger.info(
            "Starting Gemini Live session. "
            "Model: %s. Voice: %s.",
            GEMINI_LIVE_MODEL,
            GEMINI_VOICE,
        )

        await session.start(
            room=ctx.room,
            agent=support_agent,
        )

        logger.info(
            "Gemini Live session started successfully."
        )

        await send_browser_event({
            "type": "transcript",
            "sender": "Agent",
            "text": WELCOME_MESSAGE,
        })

        await session.generate_reply(
            instructions=(
                "Speak immediately. "
                f"Say exactly: {WELCOME_MESSAGE} "
                "Do not add another explanation. "
                "Wait for the customer to choose a language."
            )
        )

        logger.info(
            "Welcome message requested successfully."
        )

    except Exception:
        logger.exception(
            "Gemini Live session failed."
        )

        await send_browser_event({
            "type": "transcript",
            "sender": "System",
            "text": (
                "The voice-agent session could not start. "
                "Please check the worker logs."
            ),
        })

        raise


# =========================================================
# 16. START WORKER
# =========================================================

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
        )
    )
