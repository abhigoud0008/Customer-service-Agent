import os
import json
import logging
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, firestore

from livekit.agents import (
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    cli,
    llm,
)

try:
    from livekit.agents.pipeline import VoicePipelineAgent
except ImportError:
    from livekit.agents import VoicePipelineAgent

from livekit.plugins import google


# =========================================================
# 1. Application configuration
# =========================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# These values must be provided through Render or your .env file.
# Never write real secrets directly inside this Python file.
LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]

# Set GOOGLE_MODEL in Render to a model supported by your
# Google account and installed LiveKit Google plugin.
GOOGLE_MODEL = os.getenv(
"GOOGLE_MODEL",
"gemini-1.5-flash",
)

SMTP_SENDER_EMAIL = os.getenv(
    "SMTP_SENDER_EMAIL",
    "",
).strip()

SMTP_APP_PASSWORD = (
    os.getenv("SMTP_APP_PASSWORD", "")
    .replace(" ", "")
    .strip()
)


# =========================================================
# 2. Firebase initialization
# =========================================================

def initialize_firebase() -> None:
    """
    Initialize Firebase only once.

    In production, FIREBASE_CREDENTIALS_JSON should contain the
    complete Firebase service-account JSON document.
    """

    if firebase_admin._apps:
        return

    firebase_credentials_json = os.getenv(
        "FIREBASE_CREDENTIALS_JSON"
    )

    if firebase_credentials_json:
        try:
            credential_data = json.loads(
                firebase_credentials_json
            )

            firebase_credential = credentials.Certificate(
                credential_data
            )

        except json.JSONDecodeError as error:
            raise RuntimeError(
                "FIREBASE_CREDENTIALS_JSON is not valid JSON."
            ) from error

    else:
        # Local fallback only.
        credential_path = os.path.join(
            os.path.dirname(__file__),
            "firebase_credentials.json",
        )

        if not os.path.exists(credential_path):
            raise FileNotFoundError(
                "Firebase credentials are missing. "
                "Set FIREBASE_CREDENTIALS_JSON."
            )

        firebase_credential = credentials.Certificate(
            credential_path
        )

    firebase_admin.initialize_app(firebase_credential)


initialize_firebase()

db = firestore.client()


# =========================================================
# 3. Common helper functions
# =========================================================

def clean(value: Optional[str]) -> str:
    """
    Convert a possible None value to text and remove extra spaces.
    """

    return (value or "").strip()


def normalize_email(email: Optional[str]) -> str:
    """
    Normalize email addresses for consistent comparison.
    """

    return clean(email).lower()


def normalize_ticket_id(ticket_id: Optional[str]) -> str:
    """
    Convert ticket IDs to uppercase.

    Example:
        tick-ab1234 -> TICK-AB1234
    """

    return clean(ticket_id).upper()


def normalize_order_id(order_id: Optional[str]) -> str:
    """
    Preserve the order ID after removing surrounding spaces.
    """

    return clean(order_id)


# =========================================================
# 4. Email function
# =========================================================

def send_ticket_email_sync(
    customer_name: str,
    to_email: str,
    ticket_id: str,
    issue: str,
    status: str,
) -> bool:
    """
    Send the customer a ticket email.

    This is a synchronous function, so the agent executes it
    through asyncio.to_thread to avoid blocking audio processing.
    """

    if not SMTP_SENDER_EMAIL:
        logging.warning(
            "SMTP_SENDER_EMAIL is not configured."
        )
        return False

    if not SMTP_APP_PASSWORD:
        logging.warning(
            "SMTP_APP_PASSWORD is not configured."
        )
        return False

    if not normalize_email(to_email):
        logging.warning(
            "Customer email was not provided."
        )
        return False

    try:
        message = MIMEMultipart()

        message["From"] = (
            f"Customer Support <{SMTP_SENDER_EMAIL}>"
        )

        message["To"] = normalize_email(to_email)

        message["Subject"] = (
            f"Support Ticket #{ticket_id}"
        )

        body = (
            f"Dear {clean(customer_name) or 'Customer'},\n\n"
            "Your customer-support ticket has been registered.\n\n"
            f"Ticket ID: {ticket_id}\n"
            f"Issue: {issue}\n"
            f"Status: {status}\n\n"
            "Our support team will review your request.\n\n"
            "Regards,\n"
            "Customer Support"
        )

        message.attach(
            MIMEText(body, "plain")
        )

        with smtplib.SMTP(
            "smtp.gmail.com",
            587,
            timeout=8,
        ) as smtp_server:

            smtp_server.starttls()

            smtp_server.login(
                SMTP_SENDER_EMAIL,
                SMTP_APP_PASSWORD,
            )

            smtp_server.send_message(message)

        logging.info(
            "Ticket email sent to %s",
            normalize_email(to_email),
        )

        return True

    except Exception:
        logging.exception(
            "Ticket email could not be sent."
        )

        return False


# =========================================================
# 5. Ticket database functions
# =========================================================

def create_ticket_sync(
    customer_name: str,
    issue: str,
    email: str = "",
    order_id: str = "",
    priority: str = "NORMAL",
    status: str = "Open",
) -> dict:
    """
    Create one Firestore support ticket.
    """

    customer_name = clean(customer_name)
    issue = clean(issue)
    email = normalize_email(email)
    order_id = normalize_order_id(order_id)
    priority = clean(priority).upper() or "NORMAL"
    status = clean(status) or "Open"

    if not customer_name:
        return {
            "ok": False,
            "message": "Customer name is required.",
        }

    if not issue:
        return {
            "ok": False,
            "message": "Issue description is required.",
        }

    try:
        # Generate a Firestore document identifier.
        temporary_reference = (
            db.collection("tickets").document()
        )

        # Use part of that identifier as the readable ticket ID.
        ticket_id = (
            f"TICK-{temporary_reference.id[:8].upper()}"
        )

        ticket_reference = (
            db.collection("tickets").document(ticket_id)
        )

        ticket_data = {
            "customer_name": customer_name,
            "customer_name_lower": customer_name.lower(),
            "issue": issue,
            "email": email,
            "order_id": order_id,
            "status": status,
            "priority": priority,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }

        ticket_reference.set(ticket_data)

        logging.info(
            "Ticket %s created successfully.",
            ticket_id,
        )

        return {
            "ok": True,
            "ticket_id": ticket_id,
            "status": status,
            "priority": priority,
        }

    except Exception as error:
        logging.exception(
            "Ticket creation failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be created because "
                "the database operation failed."
            ),
            "error": str(error),
        }


def get_ticket_sync(ticket_id: str) -> dict:
    """
    Retrieve one ticket using its exact ticket ID.
    """

    ticket_id = normalize_ticket_id(ticket_id)

    if not ticket_id:
        return {
            "ok": False,
            "message": "Ticket ID is required.",
        }

    try:
        ticket_document = (
            db.collection("tickets")
            .document(ticket_id)
            .get()
        )

        if not ticket_document.exists:
            return {
                "ok": False,
                "message": (
                    f"Ticket {ticket_id} was not found."
                ),
            }

        ticket_data = ticket_document.to_dict() or {}

        return {
            "ok": True,
            "ticket": {
                "ticket_id": ticket_document.id,
                "customer_name": ticket_data.get(
                    "customer_name",
                    "Customer",
                ),
                "issue": ticket_data.get(
                    "issue",
                    "No description",
                ),
                "email": ticket_data.get(
                    "email",
                    "",
                ),
                "order_id": ticket_data.get(
                    "order_id",
                    "",
                ),
                "status": ticket_data.get(
                    "status",
                    "Open",
                ),
                "priority": ticket_data.get(
                    "priority",
                    "NORMAL",
                ),
            },
        }

    except Exception as error:
        logging.exception(
            "Ticket retrieval failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be retrieved because "
                "the database operation failed."
            ),
            "error": str(error),
        }


def find_tickets_sync(search_term: str) -> dict:
    """
    Find tickets by exact email or exact customer name.

    At most 10 tickets are returned for a short and fast response.
    """

    search_term = clean(search_term).lower()

    if not search_term:
        return {
            "ok": False,
            "message": (
                "A customer name or email is required."
            ),
            "tickets": [],
        }

    try:
        tickets_reference = db.collection("tickets")

        if "@" in search_term:
            search_field = "email"
        else:
            search_field = "customer_name_lower"

        matching_documents = (
            tickets_reference
            .where(
                search_field,
                "==",
                search_term,
            )
            .limit(10)
            .stream()
        )

        tickets = []

        for document in matching_documents:
            ticket_data = document.to_dict() or {}

            tickets.append({
                "ticket_id": document.id,
                "issue": ticket_data.get(
                    "issue",
                    "No description",
                ),
                "status": ticket_data.get(
                    "status",
                    "Open",
                ),
                "priority": ticket_data.get(
                    "priority",
                    "NORMAL",
                ),
            })

        return {
            "ok": True,
            "tickets": tickets,
        }

    except Exception as error:
        logging.exception(
            "Ticket search failed."
        )

        return {
            "ok": False,
            "message": (
                "Tickets could not be searched because "
                "the database operation failed."
            ),
            "tickets": [],
            "error": str(error),
        }


def update_ticket_sync(
    ticket_id: str,
    verification_email: str,
    new_issue: str = "",
    new_status: str = "",
) -> dict:
    """
    Update an existing ticket after verifying its email.

    Supported statuses:
        Open
        In Progress
        Resolved
        Closed
    """

    ticket_id = normalize_ticket_id(ticket_id)

    existing_result = get_ticket_sync(ticket_id)

    if not existing_result.get("ok"):
        return existing_result

    existing_ticket = existing_result["ticket"]

    saved_email = normalize_email(
        existing_ticket.get("email")
    )

    supplied_email = normalize_email(
        verification_email
    )

    if not saved_email:
        return {
            "ok": False,
            "message": (
                "This ticket does not contain a registered "
                "email and cannot be updated automatically."
            ),
        }

    if supplied_email != saved_email:
        return {
            "ok": False,
            "message": (
                "Email verification failed. "
                "The ticket was not updated."
            ),
        }

    new_issue = clean(new_issue)
    new_status = clean(new_status)

    changes = {
        "updated_at": firestore.SERVER_TIMESTAMP,
    }

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

        normalized_status = new_status.lower()

        if normalized_status not in allowed_statuses:
            return {
                "ok": False,
                "message": (
                    "Status must be Open, In Progress, "
                    "Resolved, or Closed."
                ),
            }

        changes["status"] = allowed_statuses[
            normalized_status
        ]

    if len(changes) == 1:
        return {
            "ok": False,
            "message": (
                "Provide either a new issue description "
                "or a new status."
            ),
        }

    try:
        db.collection("tickets").document(
            ticket_id
        ).update(changes)

        logging.info(
            "Ticket %s updated.",
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
        logging.exception(
            "Ticket update failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be updated because "
                "the database operation failed."
            ),
            "error": str(error),
        }


def delete_ticket_sync(
    ticket_id: str,
    verification_email: str,
) -> dict:
    """
    Permanently delete a ticket after email verification.
    """

    ticket_id = normalize_ticket_id(ticket_id)

    existing_result = get_ticket_sync(ticket_id)

    if not existing_result.get("ok"):
        return existing_result

    existing_ticket = existing_result["ticket"]

    saved_email = normalize_email(
        existing_ticket.get("email")
    )

    supplied_email = normalize_email(
        verification_email
    )

    if not saved_email:
        return {
            "ok": False,
            "message": (
                "This ticket does not contain a registered "
                "email and cannot be deleted automatically."
            ),
        }

    if supplied_email != saved_email:
        return {
            "ok": False,
            "message": (
                "Email verification failed. "
                "The ticket was not deleted."
            ),
        }

    try:
        db.collection("tickets").document(
            ticket_id
        ).delete()

        logging.info(
            "Ticket %s deleted.",
            ticket_id,
        )

        return {
            "ok": True,
            "ticket_id": ticket_id,
            "message": (
                f"Ticket {ticket_id} was deleted permanently."
            ),
        }

    except Exception as error:
        logging.exception(
            "Ticket deletion failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be deleted because "
                "the database operation failed."
            ),
            "error": str(error),
        }


# =========================================================
# 6. Order retrieval function
# =========================================================

def lookup_order_sync(order_id: str) -> dict:
    """
    Retrieve an order using the Firestore document ID.
    """

    order_id = normalize_order_id(order_id)

    if not order_id:
        return {
            "ok": False,
            "message": "Order ID is required.",
        }

    try:
        order_document = (
            db.collection("orders")
            .document(order_id)
            .get()
        )

        if not order_document.exists:
            return {
                "ok": False,
                "message": (
                    f"Order {order_id} was not found."
                ),
            }

        return {
            "ok": True,
            "order_id": order_document.id,
            "order": order_document.to_dict() or {},
        }

    except Exception as error:
        logging.exception(
            "Order retrieval failed."
        )

        return {
            "ok": False,
            "message": (
                "The order could not be retrieved because "
                "the database operation failed."
            ),
            "error": str(error),
        }


# =========================================================
# 7. AI-callable customer-service tools
# =========================================================

class SupportFunctionContext(llm.FunctionContext):
    """
    Functions inside this class are available to the AI model.
    """

    def __init__(self, session_state: dict):
        super().__init__()

        self.session_state = session_state

    @llm.ai_callable(
        description=(
            "Create a customer-support ticket. "
            "Before calling, collect the customer's full name, "
            "issue, and email. Order ID is optional."
        )
    )
    async def create_support_ticket(
        self,
        customer_name: str,
        issue: str,
        email: str,
        order_id: str = "",
    ) -> str:
        result = await asyncio.to_thread(
            create_ticket_sync,
            customer_name,
            issue,
            email,
            order_id,
            "NORMAL",
            "Open",
        )

        if not result.get("ok"):
            return result.get(
                "message",
                "The ticket could not be created.",
            )

        ticket_id = result["ticket_id"]
        status = result["status"]

        self.session_state.update({
            "customer_name": clean(customer_name),
            "email": normalize_email(email),
            "last_ticket_id": ticket_id,
            "last_issue": clean(issue),
        })

        # Email runs in the background so the voice response
        # does not wait for Gmail.
        if normalize_email(email):
            asyncio.create_task(
                asyncio.to_thread(
                    send_ticket_email_sync,
                    customer_name,
                    email,
                    ticket_id,
                    issue,
                    status,
                )
            )

        return (
            f"Ticket {ticket_id} was created. "
            f"Status is {status}."
        )

    @llm.ai_callable(
        description=(
            "Retrieve one support ticket using its exact "
            "ticket ID."
        )
    )
    async def get_support_ticket(
        self,
        ticket_id: str,
    ) -> str:
        result = await asyncio.to_thread(
            get_ticket_sync,
            ticket_id,
        )

        if not result.get("ok"):
            return result.get(
                "message",
                "The ticket could not be retrieved.",
            )

        ticket = result["ticket"]

        return (
            f"Ticket {ticket['ticket_id']}: "
            f"{ticket['issue']}. "
            f"Status {ticket['status']}. "
            f"Priority {ticket['priority']}."
        )

    @llm.ai_callable(
        description=(
            "Find up to ten support tickets using the "
            "customer's exact full name or exact email."
        )
    )
    async def find_support_tickets(
        self,
        search_term: str,
    ) -> str:
        result = await asyncio.to_thread(
            find_tickets_sync,
            search_term,
        )

        if not result.get("ok"):
            return result.get(
                "message",
                "Tickets could not be searched.",
            )

        tickets = result.get("tickets", [])

        if not tickets:
            return "No support tickets were found."

        ticket_messages = [
            (
                f"{ticket['ticket_id']}: "
                f"{ticket['status']}, "
                f"{ticket['issue']}"
            )
            for ticket in tickets
        ]

        return "; ".join(ticket_messages)

    @llm.ai_callable(
        description=(
            "Update a support ticket's issue or status. "
            "The exact ticket ID and registered email are "
            "required for verification."
        )
    )
    async def update_support_ticket(
        self,
        ticket_id: str,
        email: str,
        new_issue: str = "",
        new_status: str = "",
    ) -> str:
        result = await asyncio.to_thread(
            update_ticket_sync,
            ticket_id,
            email,
            new_issue,
            new_status,
        )

        return result.get(
            "message",
            "The ticket could not be updated.",
        )

    @llm.ai_callable(
        description=(
            "Permanently delete a support ticket. "
            "Ask for the exact ticket ID and registered email. "
            "Explain that deletion is permanent and get explicit "
            "confirmation immediately before calling this tool."
        )
    )
    async def delete_support_ticket(
        self,
        ticket_id: str,
        email: str,
        confirmed: bool = False,
    ) -> str:
        if not confirmed:
            return (
                "Deletion has not been confirmed. "
                "Ask the customer to clearly confirm permanent "
                "deletion."
            )

        result = await asyncio.to_thread(
            delete_ticket_sync,
            ticket_id,
            email,
        )

        return result.get(
            "message",
            "The ticket could not be deleted.",
        )

    @llm.ai_callable(
        description=(
            "Retrieve customer order information using "
            "the exact order ID."
        )
    )
    async def lookup_order(
        self,
        order_id: str,
    ) -> str:
        result = await asyncio.to_thread(
            lookup_order_sync,
            order_id,
        )

        if not result.get("ok"):
            return result.get(
                "message",
                "The order could not be retrieved.",
            )

        order = result["order"]

        item = order.get(
            "item",
            order.get("items", "Not listed"),
        )

        status = order.get(
            "status",
            "Processing",
        )

        delivery_date = order.get(
            "delivery_date",
            "",
        )

        answer = (
            f"Order {result['order_id']} status is {status}. "
            f"Item: {item}."
        )

        if delivery_date:
            answer += (
                f" Delivery date: {delivery_date}."
            )

        return answer

    @llm.ai_callable(
        description=(
            "Escalate a customer problem by creating a "
            "high-priority escalation ticket."
        )
    )
    async def escalate_to_human(
        self,
        customer_name: str,
        reason: str,
        email: str = "",
    ) -> str:
        result = await asyncio.to_thread(
            create_ticket_sync,
            customer_name,
            reason,
            email,
            "",
            "HIGH",
            "ESCALATED_TO_HUMAN",
        )

        if not result.get("ok"):
            return result.get(
                "message",
                "The escalation could not be created.",
            )

        ticket_id = result["ticket_id"]

        self.session_state.update({
            "customer_name": clean(customer_name),
            "email": normalize_email(email),
            "last_ticket_id": ticket_id,
            "last_issue": clean(reason),
        })

        if normalize_email(email):
            asyncio.create_task(
                asyncio.to_thread(
                    send_ticket_email_sync,
                    customer_name,
                    email,
                    ticket_id,
                    reason,
                    "ESCALATED_TO_HUMAN",
                )
            )

        return (
            f"Escalation ticket {ticket_id} was created "
            "with high priority."
        )


# =========================================================
# 8. LiveKit agent session
# =========================================================

async def entrypoint(ctx: JobContext) -> None:
    """
    Main function executed for every LiveKit agent job.
    """

    await ctx.connect(
        auto_subscribe=AutoSubscribe.AUDIO_ONLY
    )

    session_state = {
        "email": "",
        "customer_name": "Customer",
        "last_ticket_id": "",
        "last_issue": "",
    }

    agent_instructions = (
        "You are a fast customer-service voice agent. "
        "First ask which language the customer wants to use. "
        "After the customer chooses a language, continue in that "
        "language. "
        "Reply using no more than two short sentences. "
        "Use a tool immediately when you have the required values. "
        "You can create, retrieve, search, update, and delete "
        "support tickets. "
        "You can also retrieve orders and escalate issues. "
        "For ticket creation, collect customer name, issue, and "
        "email. Order ID is optional. "
        "For ticket updates, require the ticket ID and matching "
        "registered email. "
        "For deletion, require the ticket ID and matching email. "
        "Explain that deletion is permanent and obtain clear "
        "confirmation before calling the deletion tool. "
        "Never claim that a database operation succeeded unless "
        "the tool result says it succeeded. "
        "Do not reveal stored email addresses or private data."
    )

    function_context = SupportFunctionContext(
        session_state
    )

    agent = VoicePipelineAgent(
        stt=google.STT(),
        llm=google.LLM(
            model=GOOGLE_MODEL
        ),
        tts=google.TTS(
            voice_name="Aoede"
        ),
        chat_ctx=llm.ChatContext().append(
            role="system",
            text=agent_instructions,
        ),
        fnc_ctx=function_context,
    )

    async def broadcast_event(data: dict) -> None:
        """
        Send transcripts and status messages to the browser.
        """

        try:
            encoded_payload = json.dumps(
                data
            ).encode("utf-8")

            await ctx.room.local_participant.publish_data(
                encoded_payload,
                reliable=True,
            )

        except Exception:
            logging.exception(
                "LiveKit data broadcast failed."
            )

    @agent.on("agent_speech_committed")
    def on_agent_speech(message):
        asyncio.create_task(
            broadcast_event({
                "type": "transcript",
                "sender": "Agent",
                "text": message.content,
            })
        )

    @agent.on("user_speech_committed")
    def on_user_speech(message):
        asyncio.create_task(
            broadcast_event({
                "type": "transcript",
                "sender": "User",
                "text": message.content,
            })
        )

    @ctx.room.on("data_received")
    def on_data_received(data_packet):
        """
        Receive email submission or escalation data from index.html.
        """

        async def handle_data() -> None:
            try:
                payload = json.loads(
                    data_packet.data.decode("utf-8")
                )

                action = payload.get("type")

                if action == "email_submission":
                    submitted_email = normalize_email(
                        payload.get("email")
                    )

                    submitted_order_id = normalize_order_id(
                        payload.get("order_id")
                    )

                    session_state["email"] = (
                        submitted_email
                    )

                    if submitted_order_id:
                        session_state["order_id"] = (
                            submitted_order_id
                        )

                    await broadcast_event({
                        "type": "status",
                        "text": (
                            "Customer details received."
                        ),
                    })

                elif action == "manual_escalate":
                    escalation_reason = clean(
                        payload.get("reason")
                    )

                    if not escalation_reason:
                        escalation_reason = (
                            session_state["last_issue"]
                            or "Manual escalation requested"
                        )

                    escalation_result = (
                        await function_context.escalate_to_human(
                            session_state["customer_name"],
                            escalation_reason,
                            session_state["email"],
                        )
                    )

                    await broadcast_event({
                        "type": "transcript",
                        "sender": "System",
                        "text": escalation_result,
                    })

            except Exception:
                logging.exception(
                    "Browser data could not be processed."
                )

        asyncio.create_task(handle_data())

    agent.start(ctx.room)

    await agent.say(
        (
            "Welcome to customer support. "
            "Which language would you like to use?"
        ),
        allow_interruptions=True,
    )


# =========================================================
# 9. Start the LiveKit worker
# =========================================================

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="",
        )
    )
