import os
import json
import logging
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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
from livekit.agents.multimodal import MultimodalAgent
from livekit.plugins import google

load_dotenv()
logging.basicConfig(level=logging.INFO)

# ==========================================
# 1. Credentials & Firebase Setup
# ==========================================
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "wss://customer-service-agent-44fnvuqu.livekit.cloud")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "APIAXSLvd8JfCon")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "YyVvszb26v7BhwoiIeccN4MeeyuLBx0Z9zUYgaz1cEMB")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

SMTP_SENDER_EMAIL = os.getenv("SMTP_SENDER_EMAIL", "").strip()
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "").strip()

firebase_json_env = os.getenv("FIREBASE_CREDENTIALS_JSON")
if firebase_json_env:
    try:
        cred = credentials.Certificate(json.loads(firebase_json_env))
    except Exception as e:
        logging.error(f"Error parsing FIREBASE_CREDENTIALS_JSON: {e}")
        raise e
else:
    cred_path = os.path.join(os.path.dirname(__file__), "firebase_credentials.json")
    if not os.path.exists(cred_path):
        raise FileNotFoundError("Missing Firebase credentials.")
    cred = credentials.Certificate(cred_path)

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ==========================================
# 2. Non-Blocking Email Dispatch
# ==========================================
def _send_email_sync(customer_name: str, to_email: str, ticket_id: str, issue: str, status: str = "Open") -> bool:
    if not SMTP_SENDER_EMAIL or not SMTP_APP_PASSWORD or not to_email:
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = f"Customer Service <{SMTP_SENDER_EMAIL}>"
        msg["To"] = to_email
        msg["Subject"] = f"Support Ticket Registered: #{ticket_id}"

        body = (
            f"Dear {customer_name},\n\n"
            f"Your support ticket has been registered successfully.\n\n"
            f"Ticket ID: #{ticket_id}\n"
            f"Issue Reported: {issue}\n"
            f"Status: {status}\n\n"
            f"Our team will resolve this promptly.\n\n"
            f"Warm regards,\nCustomer Support Desk"
        )
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=8) as server:
            server.starttls()
            server.login(SMTP_SENDER_EMAIL, SMTP_APP_PASSWORD)
            server.send_message(msg)

        logging.info(f"Email sent successfully to {to_email}")
        return True
    except Exception as e:
        logging.error(f"SMTP Error: {e}")
        return False

# ==========================================
# 3. Synchronous Firestore Helpers
# ==========================================
def _sync_create_ticket(customer_name: str, issue: str, email: str = "", order_id: str = "", escalated: bool = False) -> dict:
    try:
        ticket_id = f"TICK-{os.urandom(3).hex().upper()}"
        status = "ESCALATED_TO_HUMAN" if escalated else "Open"
        data = {
            "customer_name": customer_name.strip(),
            "customer_name_lower": customer_name.strip().lower(),
            "issue": issue,
            "email": email.strip().lower(),
            "order_id": order_id.strip(),
            "status": status,
            "priority": "HIGH" if escalated else "NORMAL",
            "created_at": firestore.SERVER_TIMESTAMP,
        }
        db.collection("tickets").document(ticket_id).set(data)
        return {"ticket_id": ticket_id, "status": status}
    except Exception as e:
        return {"error": str(e), "ticket_id": f"TICK-{os.urandom(2).hex().upper()}", "status": "Pending"}

def _sync_lookup_order(identifier: str) -> dict:
    try:
        doc = db.collection("orders").document(identifier.strip()).get()
        if doc.exists:
            return {"found": True, "order": doc.to_dict()}
        return {"found": False}
    except Exception as e:
        return {"found": False, "error": str(e)}

def _sync_query_user_tickets(query_val: str) -> list:
    results = []
    clean_val = query_val.strip().lower()
    tickets_ref = db.collection("tickets")

    email_docs = tickets_ref.where("email", "==", clean_val).stream()
    for d in email_docs:
        data = d.to_dict()
        results.append({
            "ticket_id": d.id,
            "issue": data.get("issue", "No description"),
            "status": data.get("status", "Open")
        })

    if not results:
        name_docs = tickets_ref.where("customer_name_lower", "==", clean_val).stream()
        for d in name_docs:
            data = d.to_dict()
            results.append({
                "ticket_id": d.id,
                "issue": data.get("issue", "No description"),
                "status": data.get("status", "Open")
            })

    return results

# ==========================================
# 4. LLM Function Tools (FunctionContext)
# ==========================================
class SupportFunctionContext(llm.FunctionContext):
    @llm.ai_callable(description="Look up customer account or order details by order ID.")
    async def lookup_order(self, order_id: str) -> str:
        res = await asyncio.to_thread(_sync_lookup_order, order_id)
        if res.get("found"):
            o = res["order"]
            return f"Order details: status is {o.get('status', 'Processing')}, items: {o.get('item', 'Standard order')}."
        return f"No record found for order #{order_id}."

    @llm.ai_callable(description="Look up all tickets registered under a customer name or email address.")
    async def query_user_tickets(self, search_term: str) -> str:
        tickets = await asyncio.to_thread(_sync_query_user_tickets, search_term)
        if not tickets:
            return f"No support tickets found under {search_term}."
        total = len(tickets)
        details = ", ".join([f"Ticket #{t['ticket_id']} for {t['issue']} is {t['status']}" for t in tickets])
        return f"Found {total} ticket(s): {details}."

    @llm.ai_callable(description="Register a new customer support ticket.")
    async def create_support_ticket(self, customer_name: str, issue: str, order_id: str = "") -> str:
        res = await asyncio.to_thread(_sync_create_ticket, customer_name, issue, "", order_id, False)
        return f"Ticket created under #{res['ticket_id']}. Status: {res['status']}."

    @llm.ai_callable(description="Escalate call directly to a senior human agent or manager.")
    async def escalate_to_human(self, customer_name: str, reason: str) -> str:
        res = await asyncio.to_thread(_sync_create_ticket, customer_name, reason, "", "", True)
        return f"Escalated to supervisor under priority ticket #{res['ticket_id']}."

# ==========================================
# 5. Agent Session Execution
# ==========================================
async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    session_state = {
        "email": "",
        "customer_name": "Customer",
        "last_ticket_id": "",
        "last_issue": "",
    }

    instructions = (
        "You are an empathetic, polite, and rapid customer service agent. "
        "TONE: Speak smoothly, warmly, and respectfully at all times. "
        "LATENCY RULE: Deliver direct answers in 1 to 2 short sentences. Do not use verbose pleasantries or conversational filler. "
        "WORKFLOW: "
        "1. Warmly greet the caller, welcome them to customer support, and ask how you can help them today. "
        "2. If the user asks how many tickets they have or checks previous tickets, ask for their name or email, call 'query_user_tickets', and state the count, ticket ID, and issue clearly. "
        "3. If they report a new problem, collect their name and issue, then immediately invoke 'create_support_ticket'. Confirm the ticket ID clearly and ask them to type their email in the web box for a receipt. "
        "4. If the user asks for a human or is dissatisfied, call 'escalate_to_human'."
    )

    model = google.realtime.RealtimeModel(
        model="gemini-2.0-flash-realtime-exp",
        voice="Aoede",
        temperature=0.15,
        instructions=instructions,
    )

    agent = MultimodalAgent(
        model=model,
        fnc_ctx=SupportFunctionContext(),
    )

    async def broadcast_event(data_dict: dict):
        try:
            payload = json.dumps(data_dict).encode("utf-8")
            await ctx.room.local_participant.publish_data(payload, reliable=True)
        except Exception as e:
            logging.warning(f"Data broadcast skipped: {e}")

    # Broadcast transcripts to the browser UI
    @agent.on("agent_speech_committed")
    def on_agent_speech(msg):
        asyncio.create_task(broadcast_event({"type": "transcript", "sender": "Agent", "text": msg.content}))

    @agent.on("user_speech_committed")
    def on_user_speech(msg):
        asyncio.create_task(broadcast_event({"type": "transcript", "sender": "User", "text": msg.content}))

    # Handle incoming web details
    @ctx.room.on("data_received")
    def on_data_received(data_packet):
        try:
            payload = json.loads(data_packet.data.decode("utf-8"))
            action = payload.get("type")

            if action == "email_submission":
                email = payload.get("email", "").strip()
                session_state["email"] = email
                logging.info(f"Received customer email: {email}")

                if session_state["last_ticket_id"]:
                    asyncio.create_task(
                        asyncio.to_thread(
                            _send_email_sync,
                            session_state["customer_name"],
                            email,
                            session_state["last_ticket_id"],
                            session_state["last_issue"],
                        )
                    )
            elif action == "manual_escalate":
                asyncio.create_task(broadcast_event({
                    "type": "transcript", 
                    "sender": "System", 
                    "text": "Priority escalation requested by user."
                }))
        except Exception as err:
            logging.error(f"Error handling room data: {err}")

    # Start multimodal agent connected to this room
    await agent.start(ctx.room)

    # Trigger initial spoken greeting from the agent immediately
    try:
        session = agent.current_session
        if session:
            await session.conversation.item.create(
                llm.ChatMessage(
                    role="user",
                    content="Hello. Greet me warmly right now: say 'Welcome to Customer Support! How can I help you today?'",
                )
            )
            await session.response.create()
    except Exception as e:
        logging.warning(f"Initial welcome prompt skipped: {e}")

    # Keep worker process alive while room connection remains active
    while ctx.room.connection_state == "connected":
        await asyncio.sleep(1)

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="",
        )
    )
