# =============================
# SOLANA ARBITRAGE BOT - PRODUCTION VERSION (FREE APIs)
# Simulation mode enabled by default
# =============================

import aiohttp
import asyncio
import os
import base58
import logging
from datetime import datetime
from typing import Optional, Dict

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed

from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware

from dotenv import load_dotenv
import uvicorn

# =============================
# ENVIRONMENT
# =============================

load_dotenv()

API_KEY = os.getenv("API_KEY", "change_me")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")
RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
SIMULATION_MODE = os.getenv("SIMULATION_MODE", "true").lower() == "true"

# =============================
# CONSTANTS
# =============================

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

TRADE_AMOUNT_SOL = 0.05
MIN_PROFIT_USD = 0.10
SLIPPAGE_BPS = 100

# =============================
# LOGGING
# =============================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# =============================
# GLOBAL STATE
# =============================

bot_running = False

# =============================
# WALLET
# =============================

if PRIVATE_KEY:

    decoded = base58.b58decode(PRIVATE_KEY)

    wallet = Keypair.from_bytes(decoded)

else:

    wallet = None

# =============================
# CLIENT
# =============================

client = AsyncClient(RPC_URL)

# =============================
# TOKEN STORAGE
# =============================

tokens: Dict[str, str] = {}

# =============================
# FETCH TOKENS FROM BIRDEYE
# =============================

async def fetch_tokens():
    global tokens

    if not BIRDEYE_API_KEY:
        logging.warning("BIRDEYE_API_KEY not set")
        return

    url = "https://public-api.birdeye.so/defi/v3/token/list"

    headers = {
        "X-API-KEY": BIRDEYE_API_KEY,
        "accept": "application/json"
    }

    params = {
        "limit": 50
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:

                if resp.status != 200:
                    text = await resp.text()
                    logging.error(f"Birdeye error {resp.status}: {text}")
                    return

                data = await resp.json()

                if "data" not in data:
                    logging.error(f"Unexpected Birdeye response: {data}")
                    return

                token_data = data["data"]

                # Some v3 responses wrap items inside "items"
                if isinstance(token_data, dict) and "items" in token_data:
                    token_list = token_data["items"]
                elif isinstance(token_data, list):
                    token_list = token_data
                else:
                    logging.error(f"Unexpected token structure: {token_data}")
                    return

                tokens.clear()

                for token in token_list:
                    symbol = token.get("symbol")
                    address = token.get("address")

                    if symbol and address:
                        tokens[symbol] = address

                logging.info(f"Loaded {len(tokens)} tokens from Birdeye v3")

    except Exception as e:
        logging.error(f"fetch_tokens failed: {e}")

# =============================
# JUPITER QUOTE
# =============================

async def get_quote(session, input_mint, output_mint, amount):

    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        "slippageBps": SLIPPAGE_BPS
    }

    async with session.get(JUPITER_QUOTE_URL, params=params) as resp:

        if resp.status != 200:

            return None

        data = await resp.json()

        if "data" not in data or not data["data"]:

            return None

        return data["data"][0]

# =============================
# EXECUTE SWAP
# =============================

async def execute_swap(route):

    if SIMULATION_MODE:

        logging.info("SIMULATION: Swap skipped")

        return

    try:

        async with aiohttp.ClientSession() as session:

            payload = {
                "route": route,
                "userPublicKey": str(wallet.pubkey()),
                "wrapUnwrapSOL": True
            }

            async with session.post(JUPITER_SWAP_URL, json=payload) as resp:

                data = await resp.json()

                swap_tx = data["swapTransaction"]

                tx = VersionedTransaction.from_bytes(
                    base58.b58decode(swap_tx)
                )

                result = await client.send_transaction(
                    tx,
                    opts=TxOpts(skip_preflight=True)
                )

                logging.info(f"EXECUTED: {result.value}")

    except Exception as e:

        logging.error(f"Swap failed {e}")

# =============================
# SCAN
# =============================

async def scan():

    if not tokens:

        return

    amount = int(TRADE_AMOUNT_SOL * 1e9)

    async with aiohttp.ClientSession() as session:

        sol_price_route = await get_quote(
            session,
            SOL_MINT,
            USDC_MINT,
            amount
        )

        if not sol_price_route:

            return

        sol_price = float(sol_price_route["outAmount"]) / 1e6

        for symbol, address in tokens.items():

            try:

                buy_route = await get_quote(
                    session,
                    SOL_MINT,
                    address,
                    amount
                )

                if not buy_route:
                    continue

                token_amount = int(buy_route["outAmount"])

                sell_route = await get_quote(
                    session,
                    address,
                    SOL_MINT,
                    token_amount
                )

                if not sell_route:
                    continue

                sol_received = int(sell_route["outAmount"]) / 1e9

                profit = sol_received - TRADE_AMOUNT_SOL

                profit_usd = profit * sol_price

                logging.info(
                    f"{symbol} Profit: ${profit_usd:.4f}"
                )

                if profit_usd > MIN_PROFIT_USD:

                    logging.info(
                        f"ARBITRAGE FOUND {symbol} ${profit_usd:.4f}"
                    )

                    await execute_swap(buy_route)

            except Exception as e:

                logging.error(e)

# =============================
# BOT LOOP
# =============================

async def bot_loop():

    global bot_running

    while True:

        try:

            if bot_running:

                logging.info("Refreshing token list...")

                await fetch_tokens()

                logging.info("Scanning...")

                await scan()

        except Exception as e:

            logging.error(e)

        await asyncio.sleep(10)

# =============================
# FASTAPI
# =============================

app = FastAPI()

security = HTTPBearer()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================
# AUTH
# =============================

async def verify_key(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):

    if credentials.credentials != API_KEY:

        raise HTTPException(401)

# =============================
# ROUTES
# =============================

@app.get("/")
async def root():

    return {
        "status": "online",
        "simulation": SIMULATION_MODE
    }

@app.post("/start")
async def start(_: str = Depends(verify_key)):

    global bot_running

    bot_running = True

    return {"status": "started"}

@app.post("/stop")
async def stop(_: str = Depends(verify_key)):

    global bot_running

    bot_running = False

    return {"status": "stopped"}

@app.get("/status")
async def status():

    return {
        "running": bot_running,
        "simulation": SIMULATION_MODE
    }

# =============================
# STARTUP
# =============================

@app.on_event("startup")
async def startup():

    asyncio.create_task(bot_loop())

# =============================
# MAIN
# =============================

if __name__ == "__main__":

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000
    )



