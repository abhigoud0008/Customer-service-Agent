import os
import logging

from dotenv import load_dotenv

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
)

from livekit.plugins import google


# =========================================================
# 1. Load environment variables
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
# 2. Validate required variables
# =========================================================

REQUIRED_VARIABLES = [
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "GOOGLE_API_KEY",
]

missing_variables = [
    variable
    for variable in REQUIRED_VARIABLES
    if not os.getenv(variable)
]

if missing_variables:
    raise RuntimeError(
        "Missing required environment variables: "
        + ", ".join(missing_variables)
    )


# =========================================================
# 3. Gemini Live Model Configuration
# =========================================================

GEMINI_LIVE_MODEL = os.getenv(
    "GEMINI_LIVE_MODEL",
    "gemini-2.5-flash-native-audio-preview-12-2025",
)

GEMINI_VOICE = os.getenv(
    "GEMINI_VOICE",
    "Puck",
)


# =========================================================
# 4. Customer Support Agent
# =========================================================

class CustomerSupportAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a professional customer-service "
                "voice assistant. "

                "Always speak clearly, politely, and concisely. "

                "When the customer first joins, greet the "
                "customer and ask which language the customer "
                "would like to use. "

                "After the customer selects a language, continue "
                "the entire conversation in that language. "

                "Keep each answer to one or two short sentences. "

                "You currently provide general customer support. "
                "If the customer asks about a ticket or order, "
                "ask for the ticket ID, order ID, name, or email "
                "required to handle the request. "

                "Never remain silent after the customer speaks. "
                "If audio is unclear, politely ask the customer "
                "to repeat the request."
            )
        )


# =========================================================
# 5. LiveKit Job Entry Point
# =========================================================

async def entrypoint(
    ctx: JobContext,
) -> None:
    logger.info(
        "Agent job received for room: %s",
        ctx.room.name,
    )

    try:
        await ctx.connect()

        logger.info(
            "Agent connected to room: %s",
            ctx.room.name,
        )

        session = AgentSession(
            llm=google.realtime.RealtimeModel(
                model=GEMINI_LIVE_MODEL,
                api_key=os.environ[
                    "GOOGLE_API_KEY"
                ],
                voice=GEMINI_VOICE,
                temperature=0.7,
                instructions=(
                    "Speak naturally and respond quickly. "
                    "Use short customer-service responses. "
                    "Begin in English, then switch to the "
                    "language selected by the customer."
                ),
            )
        )

        logger.info(
            "Gemini Live session created. "
            "Model: %s. Voice: %s.",
            GEMINI_LIVE_MODEL,
            GEMINI_VOICE,
        )

        await session.start(
            room=ctx.room,
            agent=CustomerSupportAgent(),
        )

        logger.info(
            "Voice session started successfully."
        )

        await session.generate_reply(
            instructions=(
                "Immediately greet the customer by saying: "
                "'Welcome to customer support. "
                "Which language would you like to use?' "
                "Speak the greeting aloud now."
            )
        )

        logger.info(
            "Welcome message generation requested."
        )

    except Exception:
        logger.exception(
            "The voice-agent session failed."
        )
        raise


# =========================================================
# 6. Start Worker
# =========================================================

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
        )
    )
