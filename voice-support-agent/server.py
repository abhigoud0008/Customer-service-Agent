import os
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from livekit import api


# =========================================================
# Required LiveKit environment variables
# =========================================================

LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]


# =========================================================
# FastAPI application
# =========================================================

app = FastAPI(
    title="LiveKit Token Server",
    version="1.0.0",
)


# =========================================================
# CORS configuration
#
# Important:
# Origins must not have a trailing slash.
# =========================================================

allowed_origins = [
    "https://abhigoud0008.github.io",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


# =========================================================
# Health endpoint
# =========================================================

@app.get("/")
async def health_check():
    return {
        "status": "healthy",
        "service": "livekit-token-server",
        "cors_origins": allowed_origins,
    }


# =========================================================
# LiveKit token endpoint
# =========================================================

@app.get("/token")
async def get_token():
    participant_id = (
        f"user-{uuid.uuid4().hex[:8]}"
    )

    room_name = (
        f"support-{uuid.uuid4().hex[:8]}"
    )

    access_token = (
        api.AccessToken(
            LIVEKIT_API_KEY,
            LIVEKIT_API_SECRET,
        )
        .with_identity(participant_id)
        .with_name("Customer")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
    )

    return {
        "token": access_token.to_jwt(),
        "room": room_name,
        "participant": participant_id,
    }
