import os
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Allow browser calls from any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/token")
async def get_token(room: str = "support-desk", name: str = "customer"):
    api_key = os.getenv("APIaXSLvd8JfCon")
    api_secret = os.getenv("YyVvszb26v7BhwoiIeccN4MeeyuLBx0Z9zUYGAz1CEMB")

    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(name)
        .with_name(name)
        .with_grants(api.VideoGrants(room_join=True, room=room))
    )
    return {"token": token.to_jwt()}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)