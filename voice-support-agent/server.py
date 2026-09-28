import os
import uuid
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from livekit import api

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "APIAXSLvd8JfCon")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "YyVvszb26v7BhwoiIeccN4MeeyuLBx0Z9zUYgaz1cEMB")
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "wss://customer-service-agent-44fnvuqu.livekit.cloud")

@app.get("/")
async def health_check():
    """Endpoint for cron-job.org to keep Render alive with 200 OK."""
    return {"status": "healthy", "service": "livekit-token-server"}

@app.get("/token")
async def get_token():
    """Generates unique room and participant identity for every caller."""
    unique_user = f"user-{uuid.uuid4().hex[:6]}"
    unique_room = f"support-{uuid.uuid4().hex[:6]}"

    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(unique_user)
        .with_name("Customer")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=unique_room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
    )

    return {"token": token.to_jwt(), "room": unique_room}
