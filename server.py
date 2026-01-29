import aiohttp
import asyncio
import os
import base58
import logging
from datetime import datetime
from typing import Optional

from aiohttp import ClientTimeout, TCPConnector
from aiohttp.resolver import ThreadedResolver

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ====================== ENV ======================
load_dotenv()

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
PRIVATE_KEY_B58 = os.getenv("PRIVATE_KEY")
API_KEY = os.getenv("API_KEY", "change_me")

# ====================== JUPITER ======================
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"

JUPITER_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://jup.ag",
    "Referer": "https://jup.ag/",
}

# ====================== LOGGING ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# ====================== BOT ======================
class ArbitrageBot:
    def __init__(self):
        self.sol = "So11111111111111111111111111111111111111112"
        self.usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

        decoded = base58.b58decode(PRIVATE_KEY_B58)
        self.wallet = Keypair.from_bytes(decoded)

        self.client: Optional[AsyncClient] = None
        self.running = False

        self.config = {
            "slippage_bps": 100,
            "swap_amount_sol": 0.05,
            "simulate": True,
        }

    async def get_jupiter_quote(self, session, amount_lamports: int):
        params = {
            "inputMint": self.sol,
            "outputMint": self.usdc,
            "amount": amount_lamports,
            "swapMode": "ExactIn",
            "slippageBps": self.config["slippage_bps"],
            "wrapUnwrapSOL": "true",
        }

        try:
            async with session.get(
                JUPITER_QUOTE_URL,
                params=params,
                headers=JUPITER_HEADERS,
            ) as resp:
                if resp.status != 200:
                    logging.error(f"Jupiter HTTP {resp.status}")
                    return None

                data = await resp.json()
                routes = data.get("data")
                return routes[0] if routes else None

        except Exception as e:
            logging.error(f"Jupiter quote error: {e}")
            return None

# ====================== FASTAPI ======================
app = FastAPI(title="Solana Arb Bot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

bot = ArbitrageBot()
security = HTTPBearer()

async def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    if credentials.credentials != API_KEY:
        raise HTTPException(401, "Invalid API key")
    return credentials

# ====================== ROUTES ======================
@app.get("/status")
async def status():
    return {"running": bot.running}

@app.post("/start")
async def start(_: dict = Depends(verify_api_key)):
    bot.running = True
    return {"status": "started"}

@app.post("/stop")
async def stop(_: dict = Depends(verify_api_key)):
    bot.running = False
    return {"status": "stopped"}

# ====================== SANITY TEST ======================
@app.get("/jup-test")
async def jup_test():
    timeout = ClientTimeout(total=10)

    connector = TCPConnector(
        resolver=ThreadedResolver(),  # 🔥 DNS FIX
        ssl=False,
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
    ) as session:
        quote = await bot.get_jupiter_quote(
            session,
            int(0.05 * 1e9),
        )

        if not quote:
            return {"error": "No route returned"}

        return {
            "inAmount": quote["inAmount"],
            "outAmount": quote["outAmount"],
            "priceImpactPct": quote.get("priceImpactPct"),
        }



