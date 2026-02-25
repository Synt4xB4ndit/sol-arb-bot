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

from solana.rpc.async_api import AsyncClient

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

        self.tokens: Dict[str, Dict] = {}


        self.config = {

            "slippage_bps": 100,

            "swap_amount_sol": 0.05,

            "simulate": True,

        }


        self.last_token_refresh = 0


    # ====================== TOKEN DISCOVERY ======================

    async def fetch_solana_tokens(self):

        logging.info("Fetching realtime Solana tokens from DexScreener...")


        url = "https://api.dexscreener.com/latest/dex/search?q=SOL"


        try:

            async with aiohttp.ClientSession() as session:

                async with session.get(url) as resp:


                    if resp.status != 200:

                        logging.error(f"DexScreener HTTP {resp.status}")

                        return


                    data = await resp.json()


                    pairs = data.get("pairs", [])


                    new_tokens = {}


                    for pair in pairs:


                        if pair.get("chainId") != "solana":

                            continue


                        liquidity = pair.get("liquidity", {}).get("usd", 0)


                        if liquidity < 100000:

                            continue


                        symbol = pair["baseToken"]["symbol"].upper()

                        address = pair["baseToken"]["address"]


                        new_tokens[symbol] = {

                            "address": address

                        }


                    if new_tokens:

                        self.tokens = new_tokens

                        self.last_token_refresh = asyncio.get_event_loop().time()

                        logging.info(f"Loaded {len(self.tokens)} realtime tokens")


                    else:

                        logging.warning("No tokens found, keeping previous list")


        except Exception as e:

            logging.error(f"DexScreener token fetch error: {e}")


        # fallback safety

        if not self.tokens:

            logging.warning("Using fallback tokens")

            self.tokens = {

                'BONK': {'address': 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263'},

                'WIF': {'address': 'EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm'},

                'POPCAT': {'address': '7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr'},

                'TRUMP': {'address': '6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN'},

            }


    # ====================== PRICE FETCH ======================

    async def get_dexscreener_price(

        self,

        session: aiohttp.ClientSession,

        token_address: str

    ) -> Optional[float]:


        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"


        try:

            async with session.get(url) as resp:


                if resp.status != 200:

                    return None


                data = await resp.json()


                pairs = data.get("pairs", [])


                if not pairs:

                    return None


                best_pair = max(

                    pairs,

                    key=lambda p: p.get('liquidity', {}).get('usd', 0)

                )


                price = best_pair.get("priceUsd")


                return float(price) if price else None


        except:

            return None


    # ====================== SCANNER ======================

    async def scan_for_opportunities(

        self,

        session: aiohttp.ClientSession

    ):


        tokens_to_scan = list(self.tokens.items())


        for symbol, info in tokens_to_scan:


            address = info["address"]


            price = await self.get_dexscreener_price(

                session,

                address

            )


            if not price:

                continue


            logging.info(

                f"{symbol} price ${price:.6f}"

            )


    # ====================== JUPITER ======================

    async def get_jupiter_quote(

        self,

        session,

        amount_lamports: int

    ):


        params = {

            "inputMint": self.sol,

            "outputMint": self.usdc,

            "amount": amount_lamports,

            "swapMode": "ExactIn",

            "slippageBps": self.config["slippage_bps"],

        }


        try:

            async with session.get(

                JUPITER_QUOTE_URL,

                params=params,

                headers=JUPITER_HEADERS

            ) as resp:


                if resp.status != 200:

                    return None


                data = await resp.json()


                routes = data.get("data")


                return routes[0] if routes else None


        except:

            return None


# ====================== FASTAPI ======================


app = FastAPI()


app.add_middleware(

    CORSMiddleware,

    allow_origins=["*"],

    allow_methods=["*"],

    allow_headers=["*"],

)


bot = ArbitrageBot()

security = HTTPBearer()


async def verify_api_key(

    credentials:

    HTTPAuthorizationCredentials = Depends(security)

):


    if credentials.credentials != API_KEY:

        raise HTTPException(401)


    return credentials


# ====================== ROUTES ======================


@app.get("/")

async def root():

    return {

        "status": "running"

    }


@app.post("/start")

async def start(

    _: dict = Depends(verify_api_key)

):


    bot.running = True

    return {"started": True}


@app.post("/stop")

async def stop(

    _: dict = Depends(verify_api_key)

):


    bot.running = False

    return {"stopped": True}


# ====================== MAIN LOOP ======================


async def run_bot():


    logging.info("Bot started")


    while True:


        try:


            now = asyncio.get_event_loop().time()


            # refresh every 10 min


            if now - bot.last_token_refresh > 600:


                await bot.fetch_solana_tokens()


            if bot.running:


                logging.info(

                    f"[{datetime.now().strftime('%H:%M:%S')}] "

                    f"Scanning {len(bot.tokens)} tokens"

                )


                async with aiohttp.ClientSession() as session:


                    await bot.scan_for_opportunities(

                        session

                    )


        except Exception as e:


            logging.error(e)


        await asyncio.sleep(60)


# ====================== STARTUP ======================


@app.on_event("startup")

async def startup_event():


    asyncio.create_task(run_bot())


# ====================== MAIN ======================


if __name__ == "__main__":


    uvicorn.run(

        "server:app",

        host="0.0.0.0",

        port=8000

    )


