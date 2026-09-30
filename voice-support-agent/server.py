import os
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from livekit import api


# =========================================================
# 1. Required environment variables
# =========================================================

LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ[
    "LIVEKIT_API_SECRET"
]

# Multiple origins can be separated using commas.
#
# Example:
# ALLOWED_ORIGINS=http://localhost:5500,https://example.com

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5500",
    ).split(",")
    if origin.strip()
]


# =========================================================
# 2. FastAPI application
# =========================================================

app = FastAPI(
    title="LiveKit Token Server",
    version="1.0.0",
)


# =========================================================
# 3. CORS configuration
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=[
        "Content-Type",
        "Authorization",
    ],
)


# =========================================================
# 4. Health-check endpoint
# =========================================================

@app.get("/")
async def health_check():
    return {
        "status": "healthy",
        "service": "livekit-token-server",
    }


# =========================================================
# 5. LiveKit token endpoint
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
