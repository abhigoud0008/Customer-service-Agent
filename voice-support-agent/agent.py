import os
import json
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

from livekit import agents
from livekit.agents import AgentSession, Agent, JobContext, function_tool, RunContext
from livekit.plugins import google

load_dotenv()
logging.basicConfig(level=logging.INFO)

# 1. Initialize Firebase Admin SDK (Cloud Env-Var Aware)
firebase_json_env = os.getenv("FIREBASE_CREDENTIALS_JSON")

if firebase_json_env:
    # Production / Railway environment
    try:
        cred_info = json.loads(firebase_json_env)
        cred = credentials.Certificate(cred_info)
    except Exception as e:
        logging.error(f"Failed to parse FIREBASE_CREDENTIALS_JSON: {e}")
        raise e
else:
    # Local development fallback
    cred_path = os.path.join(os.path.dirname(__file__), "firebase_credentials.json")
    if not os.path.exists(cred_path):
        raise FileNotFoundError(f"Neither FIREBASE_CREDENTIALS_JSON env nor {cred_path} found.")
    cred = credentials.Certificate(cred_path)

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

# 2. Email Helper (Free Gmail SMTP)
SMTP_SENDER_EMAIL = os.getenv("SMTP_SENDER_EMAIL")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD")

def _send_email_notification(to_email: str, customer_name: str, ticket_id: str, issue: str) -> bool:
    if not SMTP_SENDER_EMAIL or not SMTP_APP_PASSWORD:
        logging.warning("SMTP credentials not provided; skipping email.")
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = f"Service Desk <{SMTP_SENDER_EMAIL}>"
        msg["To"] = to_email
        msg["Subject"] = f"Support Ticket Confirmation - #{ticket_id}"

        body = (
            f"Dear {customer_name},\n\n"
            f"Your support ticket has been registered successfully.\n\n"
            f"Ticket ID: #{ticket_id}\n"
            f"Issue Reported: {issue}\n"
            f"Status: Open\n\n"
            f"Our technical support team is processing your request.\n\n"
            f"Best regards,\nCustomer Support Team"
        )
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.starttls()
            server.login(SMTP_SENDER_EMAIL, SMTP_APP_PASSWORD)
            server.send_message(msg)

        logging.info(f"Confirmation email successfully sent to {to_email}")
        return True
    except Exception as e:
        logging.error(f"Failed to dispatch email: {e}")
        return False

# 3. Synchronous Firestore Functions
def _sync_check_and_create_ticket(customer_name: str, issue: str, email: str = "") -> dict:
    """Check for existing tickets with the same issue before creating a new one."""
    tickets_ref = db.collection("tickets")
    
    # Query open tickets for this customer
    existing_docs = tickets_ref.where("customer_name", "==", customer_name).where("status", "==", "Open").stream()
    
    for doc in existing_docs:
        data = doc.to_dict()
        existing_issue = data.get("issue", "").strip().lower()
        if existing_issue and (existing_issue in issue.lower() or issue.lower() in existing_issue):
            # Duplicate issue detected
            return {
                "status": "duplicate",
                "ticket_id": data.get("ticket_id", doc.id[:6]),
                "issue": data.get("issue")
            }

    # If not a duplicate, create a new ticket
    new_doc = tickets_ref.document()
    ticket_id = new_doc.id[:6]
    new_doc.set({
        "ticket_id": ticket_id,
        "customer_name": customer_name,
        "issue": issue,
        "email": email,
        "status": "Open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    if email:
        _send_email_notification(email, customer_name, ticket_id, issue)

    return {"status": "created", "ticket_id": ticket_id}

# 4. Agent Tools
caller_context = {
    "name": None,
    "issue": None,
    "language": "English",
}

@function_tool()
async def register_customer_issue(ctx: RunContext, customer_name: str, issue_description: str) -> str:
    """Call this tool IMMEDIATELY when the customer mentions their name and explains their issue.
    This saves their details so they can type their email."""
    caller_context["name"] = customer_name
    caller_context["issue"] = issue_description
    logging.info(f"Captured caller: {customer_name}, issue: {issue_description}")
    return "Details noted. Now ask the customer in their chosen language to enter their email in the box on their screen and click Submit."

@function_tool()
async def check_ticket_status(ctx: RunContext, ticket_id: str) -> str:
    """Look up the status of an existing ticket by its ID."""
    try:
        def _check():
            docs = db.collection("tickets").where("ticket_id", "==", ticket_id.strip()).limit(1).stream()
            for doc in docs:
                d = doc.to_dict()
                return f"Ticket #{ticket_id} is currently {d.get('status')}. Issue: {d.get('issue')}."
            return f"No ticket found with ID {ticket_id}."
        return await asyncio.to_thread(_check)
    except Exception as e:
        return "Database lookup error."

# 5. Main Entrypoint
async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=agents.AutoSubscribe.AUDIO_ONLY)
    await ctx.wait_for_participant()

    inactivity_task = None

    async def auto_disconnect_after_delay(delay: float = 8.0):
        """Wait 8 seconds; if caller remains silent, say goodbye and disconnect."""
        try:
            await asyncio.sleep(delay)
            logging.info("8 seconds elapsed with no user response. Terminating call.")
            farewell = "Thank you for contacting our customer service. Have a wonderful day. Goodbye!"
            if caller_context["language"] == "Telugu":
                farewell = "మా కస్టమర్ సర్వీస్‌ను సంప్రదించినందుకు ధన్యవాదాలు. సెలవు!"
            elif caller_context["language"] == "Marathi":
                farewell = "आमच्या ग्राहक सेवेशी संपर्क साधल्याबद्दल धन्यवाद. आपला दिवस चांगला जावो!"
            elif caller_context["language"] == "Kannada":
                farewell = "ನಮ್ಮ ಗ್ರಾಹಕ ಸೇವೆಯನ್ನು ಸಂಪರ್ಕಿಸಿದ್ದಕ್ಕಾಗಿ ಧನ್ಯವಾದಗಳು. ದಿನವು ಶುಭವಾಗಿರಲಿ!"
            elif caller_context["language"] == "Malayalam":
                farewell = "ഞങ്ങളുടെ കസ്റ്റമർ സർവീസുമായി ബന്ധപ്പെട്ടതിന് നന്ദി. നല്ലൊരു ദിവസം ആശംസിക്കുന്നു!"

            await session.generate_reply(instructions=f"Say: '{farewell}'", allow_interruptions=False)
            await asyncio.sleep(2.5)
            await ctx.room.disconnect()
        except asyncio.CancelledError:
            logging.info("Timer reset due to user activity.")

    support_agent = Agent(
        instructions=(
            "You are an ultra-fast, professional AI customer service assistant. "
            "STEP 1: The user will choose a language from English, Telugu (తెలుగు), Marathi (मराठी), Kannada (ಕನ್ನಡ), or Malayalam (മലയാളം). "
            "STEP 2: Once the language is chosen, say: "
            "'Thank you for choosing our customer service. We provide 24/7 technical repair and warranty support. "
            "Please tell me your name and the issue you are facing.' (Translate naturally into the chosen language). "
            "STEP 3: When the user shares their name and issue, invoke register_customer_issue and ask them: "
            "'Please enter your email address in the box given below on the screen and click Submit.' "
            "Keep all responses short, clear, and under 2 sentences."
        ),
        tools=[register_customer_issue, check_ticket_status],
    )

    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            voice="Aoede",
            temperature=0.25,
        ),
        min_endpointing_delay=0.3,
        max_endpointing_delay=0.9,
    )

    # Listen for email submission from frontend
    @ctx.room.on("data_received")
    def on_data_received(data_packet):
        nonlocal inactivity_task
        try:
            payload = json.loads(data_packet.data.decode("utf-8"))
            if payload.get("type") == "submit_email":
                user_email = payload.get("email")
                c_name = caller_context.get("name") or "Valued Customer"
                c_issue = caller_context.get("issue") or "Technical Support"

                result = _sync_check_and_create_ticket(
                    customer_name=c_name,
                    issue=c_issue,
                    email=user_email
                )

                if result["status"] == "duplicate":
                    reply_prompt = (
                        f"Tell the caller in their chosen language: 'You have already raised a ticket with ID {result['ticket_id']} "
                        f"for this same issue. Is there anything else I can help you with?'"
                    )
                else:
                    reply_prompt = (
                        f"Tell the caller in their chosen language: 'Your ticket has been created with ID {result['ticket_id']}, "
                        f"and a confirmation email has been sent to {user_email}. Is there anything else I can help you with?'"
                    )

                async def handle_post_ticket():
                    nonlocal inactivity_task
                    await session.generate_reply(instructions=reply_prompt, allow_interruptions=False)
                    # Start 8-second countdown after asking "anything else"
                    inactivity_task = asyncio.create_task(auto_disconnect_after_delay(8.0))

                asyncio.create_task(handle_post_ticket())
        except Exception as e:
            logging.error(f"Error handling submitted email: {e}")

    await session.start(
        room=ctx.room,
        agent=support_agent,
    )

    # Initial Prompt
    await session.generate_reply(
        instructions="Say: 'Welcome to customer support. Please select your language: English, Telugu, Marathi, Kannada, or Malayalam.'",
        allow_interruptions=False,
    )

if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
