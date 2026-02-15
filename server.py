import aiohttp
import asyncio
import os
import base58
import logging
from datetime import datetime
from typing import Optional, Dict

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
        self.tokens: Dict[str, Dict] = {}  # symbol -> {'address': str}

        self.config = {
            "slippage_bps": 100,
            "swap_amount_sol": 0.05,
            "simulate": True,
        }

    async def fetch_solana_tokens(self):
        logging.info("Fetching Solana tokens from CoinGecko (reliable mode with retry)...")
        async with aiohttp.ClientSession() as session:
            # Step 1: Get coins with platforms (usually succeeds)
            list_url = "https://api.coingecko.com/api/v3/



