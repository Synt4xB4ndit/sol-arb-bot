import os
import asyncio
import aiohttp
import base64
import logging
from fastapi import FastAPI, Header
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient

# =========================
# CONFIG
# =========================

RPC_URL = os.getenv("SOLANA_RPC_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
API_KEY = os.getenv("API_KEY")

SIMULATION_MODE = os.getenv("SIMULATION_MODE", "true").lower() == "true"

TRADE_SIZE_USD = 25
MIN_PROFIT_USD = 0.30

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"

logging.basicConfig(level=logging.INFO)

# =========================
# INIT
# =========================

app = FastAPI()

client = AsyncClient(RPC_URL)

wallet = Keypair.from_base58_string(PRIVATE_KEY)

bot_running = False

tokens = {}

# =========================
# TOKEN DISCOVERY
# =========================

async def fetch_tokens():

    url = "https://public-api.birdeye.so/defi/tokenlist"

    headers = {
        "X-API-KEY": BIRDEYE_API_KEY
    }

    async with aiohttp.ClientSession() as session:

        async with session.get(url, headers=headers) as resp:

            data = await resp.json()

            token_list = data["data"]["tokens"]

            tokens.clear()

            for token in token_list[:50]:

                tokens[token["symbol"]] = token["address"]

    logging.info(f"{len(tokens)} tokens loaded")


# =========================
# GET PRICE FROM JUPITER
# =========================

async def get_quote(session, input_mint, output_mint, amount):

    params = {

        "inputMint": input_mint,

        "outputMint": output_mint,

        "amount": amount,

        "slippageBps": 50

    }

    async with session.get(JUPITER_QUOTE_URL, params=params) as resp:

        data = await resp.json()

        if "data" not in data:

            return None

        return data["data"][0]


# =========================
# EXECUTE SWAP
# =========================

async def execute_swap(session, quote):

    if SIMULATION_MODE:

        logging.info("SIMULATION: Swap skipped")

        return True


    payload = {

        "quoteResponse": quote,

        "userPublicKey": str(wallet.pubkey()),

        "wrapUnwrapSOL": True

    }

    async with session.post(JUPITER_SWAP_URL, json=payload) as resp:

        swap_data = await resp.json()

        txn = base64.b64decode(swap_data["swapTransaction"])

        tx = txn

        result = await client.send_raw_transaction(tx)

        logging.info(result)

        return True


# =========================
# ARBITRAGE LOGIC
# =========================

async def scan():

    sol_mint = "So11111111111111111111111111111111111111112"

    async with aiohttp.ClientSession() as session:

        for symbol, mint in tokens.items():

            try:

                quote1 = await get_quote(
                    session,
                    sol_mint,
                    mint,
                    10000000
                )

                quote2 = await get_quote(
                    session,
                    mint,
                    sol_mint,
                    int(quote1["outAmount"])
                )

                profit = int(quote2["outAmount"]) - 10000000

                profit_usd = profit / 1e9 * 150

                if profit_usd > MIN_PROFIT_USD:

                    logging.info(f"PROFIT FOUND {symbol}: ${profit_usd}")

                    await execute_swap(session, quote1)

            except:

                continue


# =========================
# MAIN LOOP
# =========================

async def bot_loop():

    global bot_running

    while True:

        if bot_running:

            await fetch_tokens()

            await scan()

        await asyncio.sleep(10)


# =========================
# API CONTROL
# =========================

@app.on_event("startup")

async def startup():

    asyncio.create_task(bot_loop())


@app.post("/start")

async def start(x_api_key: str = Header()):

    global bot_running

    if x_api_key != API_KEY:

        return {"error": "unauthorized"}

    bot_running = True

    return {"status": "started"}


@app.post("/stop")

async def stop(x_api_key: str = Header()):

    global bot_running

    if x_api_key != API_KEY:

        return {"error": "unauthorized"}

    bot_running = False

    return {"status": "stopped"}


@app.get("/status")

async def status():

    return {

        "running": bot_running,

        "simulation": SIMULATION_MODE

    }


