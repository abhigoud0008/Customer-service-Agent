import os
import json
import logging
import asyncio
from typing import Any

from dotenv import load_dotenv

from livekit import rtc

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
# 1. Load configuration
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
# 2. Validate environment variables
# =========================================================

REQUIRED_ENVIRONMENT_VARIABLES = [
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "GOOGLE_API_KEY",
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

GEMINI_LIVE_MODEL = os.getenv(
    "GEMINI_LIVE_MODEL",
    "gemini-2.5-flash-native-audio-preview-12-2025",
)

GEMINI_VOICE = os.getenv(
    "GEMINI_VOICE",
    "Flare",
)


# =========================================================
# 3. Supported languages
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
# 4. Small helper functions
# =========================================================

def clean_text(
    value: Any,
) -> str:
    """
    Convert a possible missing value to cleaned text.
    """

    if value is None:
        return ""

    return str(value).strip()


def normalize_email(
    value: Any,
) -> str:
    """
    Normalize email for consistent storage and comparison.
    """

    return clean_text(
        value
    ).lower()


# =========================================================
# 5. Customer support agent
# =========================================================

class CustomerSupportAgent(Agent):
    """
    Gemini Live customer-service agent.

    customer_details is shared with the browser data-message
    handler, so the model can retrieve information submitted
    through the HTML form.
    """

    def __init__(
        self,
        customer_details: dict,
    ) -> None:
        self.customer_details = (
            customer_details
        )

        super().__init__(
            instructions=(
                "You are a fast, professional customer-service "
                "voice assistant. "

                f"The supported languages are "
                f"{SUPPORTED_LANGUAGES}. "

                "At the beginning, greet the customer and ask "
                "which supported language the customer wants. "

                "After the customer selects a language, speak "
                "only in that language unless the customer asks "
                "to switch. "

                "Use short, natural answers. "
                "Use no more than one or two short sentences. "

                "Respond immediately after the customer finishes "
                "speaking. Do not add unnecessary introductions, "
                "explanations, summaries, or filler words. "

                "If the customer submits account information "
                "through the website, do not repeatedly ask for "
                "the same name, email, or order ID. "

                "Before asking for the customer's name, email, "
                "or order ID, call the get_submitted_details tool. "

                "If the tool shows that a required value has "
                "already been submitted, use it. "

                "Never read the complete customer email address "
                "out loud. You may say that the registered email "
                "was received. "

                "If the submitted details are incomplete, ask "
                "only for the missing value. "

                "If customer audio is unclear, ask the customer "
                "to repeat the request once. "

                "Never remain silent after valid customer speech."
            )
        )

    @function_tool
    async def get_submitted_details(
        self,
    ) -> str:
        """
        Retrieve customer details submitted through the website.

        The agent must use this before asking the customer
        repeatedly for name, email, account, or order information.
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

        order_id = clean_text(
            self.customer_details.get(
                "order_id"
            )
        )

        available_details = []

        if customer_name:
            available_details.append(
                f"customer name is {customer_name}"
            )

        if email:
            available_details.append(
                "a registered email address was received"
            )

        if order_id:
            available_details.append(
                f"order or account ID is {order_id}"
            )

        if not available_details:
            return (
                "No customer details have been submitted "
                "through the website."
            )

        return (
            "The website has already supplied these details: "
            + "; ".join(
                available_details
            )
            + ". Do not ask for these values again."
        )


# =========================================================
# 6. LiveKit agent entry point
# =========================================================

async def entrypoint(
    ctx: JobContext,
) -> None:
    """
    Run one realtime customer-support session.
    """

    logger.info(
        "Agent job received for room: %s",
        ctx.room.name,
    )

    customer_details = {
        "customer_name": "",
        "email": "",
        "order_id": "",
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
    # Send custom events to the existing transcript box
    # =====================================================

    async def send_browser_event(
        event_data: dict,
    ) -> None:
        """
        Send JSON data to index.html.

        index.html already handles:
            transcript
            status
            ticket_created
        """

        try:
            encoded_event = json.dumps(
                event_data,
                ensure_ascii=False,
            ).encode("utf-8")

            await (
                ctx.room
                .local_participant
                .publish_data(
                    encoded_event,
                    reliable=True,
                )
            )

        except Exception:
            logger.exception(
                "Browser event could not be published."
            )


    # =====================================================
    # Create Gemini Live session
    # =====================================================

    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=GEMINI_LIVE_MODEL,
            api_key=GOOGLE_API_KEY,
            voice=GEMINI_VOICE,

            # Lower temperature makes responses more direct
            # and avoids unnecessary long answers.
            temperature=0.2,

            instructions=(
                "Respond as quickly as possible. "
                "Use at most two short sentences. "
                "Do not repeat the customer's message. "
                "Do not use long introductions. "
                "Do not explain internal processing. "
                "Speak naturally and clearly."
            ),
        )
    )

    support_agent = CustomerSupportAgent(
        customer_details
    )


    # =====================================================
    # User transcript events
    # =====================================================

    @session.on(
        "user_input_transcribed"
    )
    def on_user_input_transcribed(
        event: UserInputTranscribedEvent,
    ) -> None:
        """
        Forward final customer transcripts to index.html.
        """

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
    # Agent transcript events
    # =====================================================

    @session.on(
        "conversation_item_added"
    )
    def on_conversation_item_added(
        event: ConversationItemAddedEvent,
    ) -> None:
        """
        Forward committed agent messages to index.html.

        User messages are handled by user_input_transcribed,
        so only agent text is sent from this event.
        """

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
    # Receive submitted name, email, and order information
    # =====================================================

    @ctx.room.on(
        "data_received"
    )
    def on_data_received(
        data_packet: rtc.DataPacket,
    ) -> None:
        """
        Receive JSON submitted from index.html.
        """

        async def process_data_packet() -> None:
            try:
                payload_text = (
                    data_packet.data
                    .decode("utf-8")
                )

                payload = json.loads(
                    payload_text
                )

                action = clean_text(
                    payload.get("type")
                )

                logger.info(
                    "Browser data action received: %s",
                    action,
                )

                if action == "email_submission":
                    submitted_name = clean_text(
                        payload.get(
                            "customer_name"
                        )
                    )

                    submitted_email = (
                        normalize_email(
                            payload.get(
                                "email"
                            )
                        )
                    )

                    submitted_order_id = (
                        clean_text(
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
                        "Customer website details updated. "
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

                    # Tell the model immediately that details
                    # are now available in its tool.
                    session.generate_reply(
                        instructions=(
                            "The customer just submitted account "
                            "details through the website. "
                            "Call get_submitted_details now. "
                            "Briefly confirm that the details were "
                            "received. Do not ask for any value "
                            "that was already supplied, and do not "
                            "read the complete email address aloud."
                        )
                    )

                elif action == "manual_escalate":
                    await send_browser_event({
                        "type": "transcript",
                        "sender": "System",
                        "text": (
                            "Your escalation request "
                            "was received."
                        ),
                    })

                    session.generate_reply(
                        instructions=(
                            "The customer requested escalation. "
                            "Briefly confirm that the escalation "
                            "request was received."
                        )
                    )

            except Exception:
                logger.exception(
                    "Browser data packet could not be processed."
                )

        asyncio.create_task(
            process_data_packet()
        )


    # =====================================================
    # Start the Gemini Live voice session
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
            "Gemini Live session started."
        )


        # Show the greeting immediately in the browser.
        await send_browser_event({
            "type": "transcript",
            "sender": "Agent",
            "text": WELCOME_MESSAGE,
        })


        # Ask the realtime model to speak immediately.
        await session.generate_reply(
            instructions=(
                "Speak immediately. "
                f"Say this welcome message: {WELCOME_MESSAGE} "
                "Do not add any other explanation. "
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
                "The voice agent could not start. "
                "Please check the agent worker logs."
            ),
        })

        raise


# =========================================================
# 7. Start worker
# =========================================================

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
        )
    )
