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

from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import google, silero


# =========================================================
# 1. INITIAL CONFIGURATION
# =========================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(
    "customer-support-agent"
)


# =========================================================
# 2. ENVIRONMENT VARIABLES
# =========================================================

LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]

GOOGLE_MODEL = os.getenv(
    "GOOGLE_MODEL",
    "gemini-1.5-flash",
)

GOOGLE_TTS_VOICE = os.getenv(
    "GOOGLE_TTS_VOICE",
    "en-US-Standard-H",
)

GOOGLE_SPEECH_LANGUAGE = os.getenv(
    "GOOGLE_SPEECH_LANGUAGE",
    "en-US",
)

FIREBASE_CREDENTIALS_JSON = os.getenv(
    "FIREBASE_CREDENTIALS_JSON",
    "",
).strip()

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
# 3. GOOGLE CLOUD CREDENTIALS
# =========================================================

def configure_google_credentials() -> None:
    """
    Configure Google Cloud authentication for STT and TTS.

    GOOGLE_API_KEY is used by Gemini.

    Google Cloud Speech-to-Text and Text-to-Speech require
    service-account credentials.
    """

    existing_credentials = os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )

    if existing_credentials:
        logger.info(
            "Using existing Google credentials file: %s",
            existing_credentials,
        )
        return

    if not FIREBASE_CREDENTIALS_JSON:
        raise RuntimeError(
            "FIREBASE_CREDENTIALS_JSON is missing. "
            "Google Cloud STT and TTS require service-account "
            "credentials."
        )

    try:
        credential_data = json.loads(
            FIREBASE_CREDENTIALS_JSON
        )

        credential_path = (
            "/tmp/google_service_account.json"
        )

        with open(
            credential_path,
            "w",
            encoding="utf-8",
        ) as credential_file:
            json.dump(
                credential_data,
                credential_file,
            )

        os.environ[
            "GOOGLE_APPLICATION_CREDENTIALS"
        ] = credential_path

        logger.info(
            "Google Cloud credentials configured successfully."
        )

    except Exception:
        logger.exception(
            "Google Cloud credentials could not be configured."
        )
        raise


configure_google_credentials()


# =========================================================
# 4. FIREBASE INITIALIZATION
# =========================================================

def initialize_firebase() -> None:
    """
    Initialize Firebase only once.
    """

    if firebase_admin._apps:
        return

    if FIREBASE_CREDENTIALS_JSON:
        try:
            credential_data = json.loads(
                FIREBASE_CREDENTIALS_JSON
            )

            firebase_credential = (
                credentials.Certificate(
                    credential_data
                )
            )

        except json.JSONDecodeError as error:
            raise RuntimeError(
                "FIREBASE_CREDENTIALS_JSON is invalid JSON."
            ) from error

    else:
        local_credential_path = os.path.join(
            os.path.dirname(__file__),
            "firebase_credentials.json",
        )

        if not os.path.exists(
            local_credential_path
        ):
            raise FileNotFoundError(
                "Firebase credentials are missing. "
                "Set FIREBASE_CREDENTIALS_JSON."
            )

        firebase_credential = (
            credentials.Certificate(
                local_credential_path
            )
        )

    firebase_admin.initialize_app(
        firebase_credential
    )

    logger.info(
        "Firebase initialized successfully."
    )


initialize_firebase()

db = firestore.client()


# =========================================================
# 5. COMMON HELPERS
# =========================================================

def clean(
    value: Optional[str],
) -> str:
    return (value or "").strip()


def normalize_email(
    email: Optional[str],
) -> str:
    return clean(email).lower()


def normalize_ticket_id(
    ticket_id: Optional[str],
) -> str:
    return clean(ticket_id).upper()


def normalize_order_id(
    order_id: Optional[str],
) -> str:
    return clean(order_id).upper()


# =========================================================
# 6. EMAIL
# =========================================================

def send_ticket_email_sync(
    customer_name: str,
    recipient_email: str,
    ticket_id: str,
    issue: str,
    status: str,
) -> bool:
    """
    Send a ticket confirmation email.
    """

    recipient_email = normalize_email(
        recipient_email
    )

    if not SMTP_SENDER_EMAIL:
        logger.warning(
            "SMTP_SENDER_EMAIL is not configured."
        )
        return False

    if not SMTP_APP_PASSWORD:
        logger.warning(
            "SMTP_APP_PASSWORD is not configured."
        )
        return False

    if not recipient_email:
        return False

    try:
        message = MIMEMultipart()

        message["From"] = (
            f"Customer Support "
            f"<{SMTP_SENDER_EMAIL}>"
        )

        message["To"] = recipient_email

        message["Subject"] = (
            f"Support Ticket #{ticket_id}"
        )

        message_body = (
            f"Dear {clean(customer_name) or 'Customer'},\n\n"
            "Your support ticket has been registered.\n\n"
            f"Ticket ID: {ticket_id}\n"
            f"Issue: {issue}\n"
            f"Status: {status}\n\n"
            "Our support team will review your request.\n\n"
            "Regards,\n"
            "Customer Support"
        )

        message.attach(
            MIMEText(
                message_body,
                "plain",
            )
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

            smtp_server.send_message(
                message
            )

        logger.info(
            "Ticket email sent successfully."
        )

        return True

    except Exception:
        logger.exception(
            "Ticket email could not be sent."
        )
        return False


# =========================================================
# 7. CREATE TICKET
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
    Create a support ticket in Firestore.
    """

    customer_name = clean(
        customer_name
    )

    issue = clean(issue)

    email = normalize_email(
        email
    )

    order_id = normalize_order_id(
        order_id
    )

    priority = (
        clean(priority).upper()
        or "NORMAL"
    )

    status = (
        clean(status)
        or "Open"
    )

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
        temporary_reference = (
            db.collection("tickets")
            .document()
        )

        ticket_id = (
            f"TICK-"
            f"{temporary_reference.id[:8].upper()}"
        )

        ticket_reference = (
            db.collection("tickets")
            .document(ticket_id)
        )

        ticket_reference.set({
            "customer_name": customer_name,
            "customer_name_lower": (
                customer_name.lower()
            ),
            "issue": issue,
            "email": email,
            "order_id": order_id,
            "status": status,
            "priority": priority,
            "created_at": (
                firestore.SERVER_TIMESTAMP
            ),
            "updated_at": (
                firestore.SERVER_TIMESTAMP
            ),
        })

        logger.info(
            "Ticket %s created.",
            ticket_id,
        )

        return {
            "ok": True,
            "ticket_id": ticket_id,
            "status": status,
            "priority": priority,
        }

    except Exception as error:
        logger.exception(
            "Ticket creation failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be created."
            ),
            "error": str(error),
        }


# =========================================================
# 8. RETRIEVE ONE TICKET
# =========================================================

def get_ticket_sync(
    ticket_id: str,
) -> dict:
    """
    Retrieve one support ticket.
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
            db.collection("tickets")
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

        ticket_data = (
            document.to_dict() or {}
        )

        return {
            "ok": True,
            "ticket": {
                "ticket_id": document.id,
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
        logger.exception(
            "Ticket retrieval failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be retrieved."
            ),
            "error": str(error),
        }


# =========================================================
# 9. FIND CUSTOMER TICKETS
# =========================================================

def find_tickets_sync(
    search_term: str,
) -> dict:
    """
    Find tickets using an exact name or email.
    """

    search_term = clean(
        search_term
    ).lower()

    if not search_term:
        return {
            "ok": False,
            "message": (
                "A customer name or email is required."
            ),
            "tickets": [],
        }

    try:
        tickets_reference = (
            db.collection("tickets")
        )

        if "@" in search_term:
            search_field = "email"
        else:
            search_field = (
                "customer_name_lower"
            )

        documents = (
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

        for document in documents:
            ticket_data = (
                document.to_dict() or {}
            )

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
        logger.exception(
            "Ticket search failed."
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
# 10. UPDATE TICKET
# =========================================================

def update_ticket_sync(
    ticket_id: str,
    verification_email: str,
    new_issue: str = "",
    new_status: str = "",
) -> dict:
    """
    Update a ticket after email verification.
    """

    ticket_id = normalize_ticket_id(
        ticket_id
    )

    existing_result = get_ticket_sync(
        ticket_id
    )

    if not existing_result.get("ok"):
        return existing_result

    existing_ticket = (
        existing_result["ticket"]
    )

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
                "This ticket does not have a registered email."
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

    changes = {
        "updated_at": (
            firestore.SERVER_TIMESTAMP
        ),
    }

    new_issue = clean(
        new_issue
    )

    new_status = clean(
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
                    "Status must be Open, In Progress, "
                    "Resolved, or Closed."
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
        db.collection("tickets").document(
            ticket_id
        ).update(changes)

        return {
            "ok": True,
            "message": (
                f"Ticket {ticket_id} was updated."
            ),
        }

    except Exception as error:
        logger.exception(
            "Ticket update failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be updated."
            ),
            "error": str(error),
        }


# =========================================================
# 11. DELETE TICKET
# =========================================================

def delete_ticket_sync(
    ticket_id: str,
    verification_email: str,
) -> dict:
    """
    Permanently delete a verified ticket.
    """

    ticket_id = normalize_ticket_id(
        ticket_id
    )

    existing_result = get_ticket_sync(
        ticket_id
    )

    if not existing_result.get("ok"):
        return existing_result

    existing_ticket = (
        existing_result["ticket"]
    )

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
                "This ticket does not have a "
                "registered email."
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

        return {
            "ok": True,
            "message": (
                f"Ticket {ticket_id} was "
                "deleted permanently."
            ),
        }

    except Exception as error:
        logger.exception(
            "Ticket deletion failed."
        )

        return {
            "ok": False,
            "message": (
                "The ticket could not be deleted."
            ),
            "error": str(error),
        }


# =========================================================
# 12. ORDER LOOKUP
# =========================================================

def lookup_order_sync(
    order_id: str,
) -> dict:
    """
    Retrieve an order using its exact document ID.
    """

    order_id = normalize_order_id(
        order_id
    )

    if not order_id:
        return {
            "ok": False,
            "message": "Order ID is required.",
        }

    try:
        document = (
            db.collection("orders")
            .document(order_id)
            .get()
        )

        if not document.exists:
            return {
                "ok": False,
                "message": (
                    f"Order {order_id} was not found."
                ),
            }

        return {
            "ok": True,
            "order_id": document.id,
            "order": document.to_dict() or {},
        }

    except Exception as error:
        logger.exception(
            "Order retrieval failed."
        )

        return {
            "ok": False,
            "message": (
                "The order could not be retrieved."
            ),
            "error": str(error),
        }


# =========================================================
# 13. AI-CALLABLE TOOLS
# =========================================================

class SupportFunctionContext(
    llm.FunctionContext
):
    def __init__(
        self,
        session_state: dict,
    ):
        super().__init__()

        self.session_state = (
            session_state
        )

    @llm.ai_callable(
        description=(
            "Create a support ticket. Collect the customer's "
            "name, issue, and email before calling. "
            "Order ID is optional."
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
                "Ticket creation failed.",
            )

        ticket_id = result[
            "ticket_id"
        ]

        status = result["status"]

        self.session_state.update({
            "customer_name": clean(
                customer_name
            ),
            "email": normalize_email(
                email
            ),
            "last_ticket_id": ticket_id,
            "last_issue": clean(issue),
        })

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
            "Retrieve one support ticket using its "
            "exact ticket ID."
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
                "Ticket retrieval failed.",
            )

        ticket = result["ticket"]

        return (
            f"Ticket {ticket['ticket_id']}. "
            f"Issue: {ticket['issue']}. "
            f"Status: {ticket['status']}. "
            f"Priority: {ticket['priority']}."
        )

    @llm.ai_callable(
        description=(
            "Find support tickets using the customer's "
            "exact name or email."
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
                "Ticket search failed.",
            )

        tickets = result.get(
            "tickets",
            [],
        )

        if not tickets:
            return (
                "No support tickets were found."
            )

        descriptions = [
            (
                f"{ticket['ticket_id']}: "
                f"{ticket['status']}, "
                f"{ticket['issue']}"
            )
            for ticket in tickets
        ]

        return "; ".join(
            descriptions
        )

    @llm.ai_callable(
        description=(
            "Update a ticket's issue or status. Require "
            "the ticket ID and registered email."
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
            "Ticket update failed.",
        )

    @llm.ai_callable(
        description=(
            "Permanently delete a ticket. Require the exact "
            "ticket ID, registered email, and explicit "
            "customer confirmation."
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
                "Deletion is not confirmed. "
                "Ask the customer to confirm permanent deletion."
            )

        result = await asyncio.to_thread(
            delete_ticket_sync,
            ticket_id,
            email,
        )

        return result.get(
            "message",
            "Ticket deletion failed.",
        )

    @llm.ai_callable(
        description=(
            "Retrieve order details using an exact order ID."
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
                "Order retrieval failed.",
            )

        order = result["order"]

        item = order.get(
            "item",
            order.get(
                "items",
                "Not listed",
            ),
        )

        status = order.get(
            "status",
            "Processing",
        )

        delivery_date = order.get(
            "delivery_date",
            "",
        )

        response = (
            f"Order {result['order_id']} "
            f"status is {status}. "
            f"Item: {item}."
        )

        if delivery_date:
            response += (
                f" Delivery date: "
                f"{delivery_date}."
            )

        return response

    @llm.ai_callable(
        description=(
            "Escalate an issue by creating a high-priority "
            "support ticket."
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
                "Escalation failed.",
            )

        ticket_id = result[
            "ticket_id"
        ]

        self.session_state.update({
            "customer_name": clean(
                customer_name
            ),
            "email": normalize_email(
                email
            ),
            "last_ticket_id": ticket_id,
            "last_issue": clean(reason),
        })

        return (
            f"Escalation ticket {ticket_id} "
            "was created with high priority."
        )


# =========================================================
# 14. PREWARM SILERO
# =========================================================

def prewarm_process(proc) -> None:
    """
    Load Silero VAD before accepting calls.
    """

    logger.info(
        "Loading Silero VAD."
    )

    proc.userdata["vad"] = (
        silero.VAD.load()
    )

    logger.info(
        "Silero VAD loaded successfully."
    )


# =========================================================
# 15. LIVEKIT AGENT SESSION
# =========================================================

async def entrypoint(
    ctx: JobContext,
) -> None:
    """
    Start one customer-support voice session.
    """

    logger.info(
        "Agent job received for room: %s",
        ctx.room.name,
    )

    try:
        await ctx.connect(
            auto_subscribe=(
                AutoSubscribe.AUDIO_ONLY
            )
        )

        logger.info(
            "Agent connected to room: %s",
            ctx.room.name,
        )

        # Wait for the customer to join the room.
        participant = await asyncio.wait_for(
            ctx.wait_for_participant(),
            timeout=30,
        )

        logger.info(
            "Customer participant detected: %s",
            participant.identity,
        )

    except asyncio.TimeoutError:
        logger.error(
            "No customer participant joined within 30 seconds."
        )
        return

    except Exception:
        logger.exception(
            "Agent could not connect to LiveKit."
        )
        raise

    session_state = {
        "email": "",
        "customer_name": "Customer",
        "last_ticket_id": "",
        "last_issue": "",
        "order_id": "",
    }

    instructions = (
        "You are a professional and fast customer-service "
        "voice agent. "
        "First ask which language the customer prefers. "
        "Continue in the selected language. "
        "Keep answers concise and use no more than two short "
        "sentences. "
        "Use tools immediately after collecting the required "
        "information. "
        "You can create, retrieve, find, update, and delete "
        "support tickets. "
        "You can retrieve order details and escalate issues. "
        "For ticket creation, collect the customer's name, "
        "issue, and email. "
        "For updates and deletion, require the exact ticket ID "
        "and registered email. "
        "Before deletion, explain that deletion is permanent "
        "and obtain explicit confirmation. "
        "Never report success unless the tool confirms success."
    )

    function_context = (
        SupportFunctionContext(
            session_state
        )
    )

    try:
        logger.info(
            "Creating voice pipeline. "
            "Model: %s. Voice: %s.",
            GOOGLE_MODEL,
            GOOGLE_TTS_VOICE,
        )

        agent = VoicePipelineAgent(
            vad=ctx.proc.userdata[
                "vad"
            ],

            stt=google.STT(),

            llm=google.LLM(
                model=GOOGLE_MODEL,
            ),

            tts=google.TTS(
                language=(
                    GOOGLE_SPEECH_LANGUAGE
                ),
                voice_name=(
                    GOOGLE_TTS_VOICE
                ),
            ),

            chat_ctx=(
                llm.ChatContext()
                .append(
                    role="system",
                    text=instructions,
                )
            ),

            fnc_ctx=function_context,
        )

        logger.info(
            "Voice pipeline created successfully."
        )

    except Exception:
        logger.exception(
            "Voice pipeline initialization failed."
        )
        raise

    async def broadcast_event(
        event: dict,
    ) -> None:
        try:
            payload = json.dumps(
                event
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
                "LiveKit data broadcast failed."
            )

    @agent.on(
        "agent_speech_committed"
    )
    def on_agent_speech(
        message,
    ):
        asyncio.create_task(
            broadcast_event({
                "type": "transcript",
                "sender": "Agent",
                "text": message.content,
            })
        )

    @agent.on(
        "user_speech_committed"
    )
    def on_user_speech(
        message,
    ):
        asyncio.create_task(
            broadcast_event({
                "type": "transcript",
                "sender": "User",
                "text": message.content,
            })
        )

    @ctx.room.on(
        "data_received"
    )
    def on_data_received(
        data_packet,
    ):
        async def handle_data() -> None:
            try:
                payload = json.loads(
                    data_packet.data.decode(
                        "utf-8"
                    )
                )

                action = payload.get(
                    "type"
                )

                if (
                    action ==
                    "email_submission"
                ):
                    session_state[
                        "email"
                    ] = normalize_email(
                        payload.get("email")
                    )

                    session_state[
                        "order_id"
                    ] = normalize_order_id(
                        payload.get(
                            "order_id"
                        )
                    )

                    await broadcast_event({
                        "type": "status",
                        "message": (
                            "Customer details received."
                        ),
                    })

                elif (
                    action ==
                    "manual_escalate"
                ):
                    reason = clean(
                        payload.get("reason")
                    )

                    if not reason:
                        reason = (
                            session_state[
                                "last_issue"
                            ]
                            or (
                                "Manual escalation "
                                "requested"
                            )
                        )

                    result = await (
                        function_context
                        .escalate_to_human(
                            session_state[
                                "customer_name"
                            ],
                            reason,
                            session_state[
                                "email"
                            ],
                        )
                    )

                    await broadcast_event({
                        "type": "transcript",
                        "sender": "System",
                        "text": result,
                    })

            except Exception:
                logger.exception(
                    "Browser data could not be processed."
                )

        asyncio.create_task(
            handle_data()
        )

    welcome_message = (
        "Welcome to customer support. "
        "Which language would you like to use?"
    )

    try:
        logger.info(
            "Starting the voice pipeline for participant: %s",
            participant.identity,
        )

        # Bind the voice pipeline to the customer.
        agent.start(
            ctx.room,
            participant,
        )

        logger.info(
            "Voice pipeline started successfully."
        )

        # Send transcript first so the question is visible
        # even if TTS has a provider problem.
        await broadcast_event({
            "type": "transcript",
            "sender": "Agent",
            "text": welcome_message,
        })

        logger.info(
            "Welcome transcript sent. Starting TTS."
        )

        await asyncio.wait_for(
            agent.say(
                welcome_message,
                allow_interruptions=True,
            ),
            timeout=30,
        )

        logger.info(
            "Welcome audio completed successfully."
        )

    except asyncio.TimeoutError:
        logger.error(
            "Welcome TTS timed out after 30 seconds."
        )

        await broadcast_event({
            "type": "transcript",
            "sender": "System",
            "text": (
                "The voice service timed out. "
                "Please reconnect."
            ),
        })

    except Exception as error:
        logger.exception(
            "Agent failed while generating "
            "the welcome message."
        )

        await broadcast_event({
            "type": "transcript",
            "sender": "System",
            "text": (
                "Voice initialization failed: "
                f"{type(error).__name__}. "
                "Please check the agent worker logs."
            ),
        })


# =========================================================
# 16. START WORKER
# =========================================================

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm_process,
            agent_name="",
        )
    )
