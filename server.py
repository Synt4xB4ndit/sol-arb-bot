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

        logging.info("Fetching Solana tokens from CoinGecko (correct method)...")

        url = "https://api.coingecko.com/api/v3/coins/markets"

        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": 1,
            "sparkline": "false",
        }

        headers = {
            "Accept": "application/json",
            "User-Agent": "SolanaArbBot/1.0"
        }

        async with aiohttp.ClientSession(headers=headers) as session:

            for attempt in range(5):

                try:

                    async with session.get(url, params=params) as resp:

                        if resp.status == 429:

                            wait = 60 * (attempt + 1)

                            logging.warning(f"429 received. Waiting {wait}s...")
                            await asyncio.sleep(wait)
                            continue


                        if resp.status != 200:

                            logging.error(f"CoinGecko error: {resp.status}")
                            continue


                        data = await resp.json()

                        logging.info(f"Received {len(data)} market tokens")

                        self.tokens = {}

                        for coin in data:

                            market_cap = coin.get("market_cap", 0)

                            if market_cap < 1_000_000:
                                continue

                            platforms = coin.get("platforms", {})

                            sol_address = platforms.get("solana")

                            if not sol_address:
                                continue

                            symbol = coin["symbol"].upper()

                            self.tokens[symbol] = {
                                "address": sol_address
                            }


                        logging.info(f"Loaded {len(self.tokens)} Solana tokens successfully")

                        if self.tokens:
                            return


                except Exception as e:

                    logging.error(f"Error fetching tokens: {e}")

                    await asyncio.sleep(30)


        logging.warning("Using fallback tokens")

        self.tokens = {
            'BONK': {'address': 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263'},
            'WIF': {'address': 'EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm'},
            'POPCAT': {'address': '7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr'},
            'TRUMP': {'address': '6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN'},
        }

    async def get_dexscreener_price(self, session: aiohttp.ClientSession, token_address: str) -> Optional[float]:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logging.debug(f"DexScreener status {resp.status} for {token_address}")
                    return None
                data = await resp.json()
                if 'pairs' not in data or not data['pairs']:
                    return None
                # Prefer Raydium pair with highest liquidity
                for pair in sorted(data['pairs'], key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True):
                    if pair.get('chainId') == 'solana' and pair.get('dexId') == 'raydium' and pair.get('priceUsd'):
                        return float(pair['priceUsd'])
                # Fallback to any Solana pair
                for pair in data['pairs']:
                    if pair.get('chainId') == 'solana' and pair.get('priceUsd'):
                        return float(pair['priceUsd'])
                return None
        except Exception as e:
            logging.error(f"DexScreener error for {token_address}: {e}")
            return None

    async def scan_for_opportunities(self, session: aiohttp.ClientSession):
        # Limit to top 30 tokens (can sort by liquidity/volume later)
        tokens_to_scan = list(self.tokens.items())[:30]

        for symbol, info in tokens_to_scan:
            address = info['address']
        
            ds_price = await self.get_dexscreener_price(session, address)
            if ds_price is None:
                continue

            # Get liquidity from DexScreener pair (add filter)
            liquidity_usd = 0
            try:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if 'pairs' in data and data['pairs']:
                            # Take best Raydium pair liquidity
                            raydium_pair = next((p for p in data['pairs'] if p.get('dexId') == 'raydium'), None)
                            if raydium_pair:
                                liquidity_usd = raydium_pair.get('liquidity', {}).get('usd', 0)
            except Exception as e:
                logging.debug(f"Liquidity fetch error for {symbol}: {e}")

            # Skip low liquidity (< $50k example)
            if liquidity_usd < 50000:
                logging.debug(f"Skipping {symbol} - low liquidity ${liquidity_usd:,.0f}")
                continue

            baseline_price = ds_price
            diff_pct = 0.0  # Placeholder - real diff when Jupiter fixed
            logging.info(f"{symbol}: DS ${ds_price:.6f} (baseline) | Liquidity ${liquidity_usd:,.0f}")

            # Example: flag high-potential tokens (expand when Jupiter works)
            if liquidity_usd > 100000:  # example filter for better ops
                logging.info(f"High-potential token {symbol} - Liquidity ${liquidity_usd:,.0f}")
                # TODO: add real diff check and arb execution here

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

@app.get("/")
async def root():
    return {"message": "Solana arbitrage bot is running", "owner": "DEV"}

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
        resolver=ThreadedResolver(),  # DNS FIX
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

# ====================== BACKGROUND BOT LOOP ======================
async def run_bot():
    #Fetch tokens only once at startup
    if not bot.tokens:
        await bot.fetch_solana_tokens()
    logging.info(f"Bot startup complete with {len(bot.tokens)} tokens")

    while True:
        if bot.running:
            logging.info(f"[{datetime.now().strftime('%H:%M:%S')}] Scanning {len(bot.tokens)} tokens...")
            async with aiohttp.ClientSession() as session:
                await bot.scan_for_opportunities(session)
        await asyncio.sleep(180) #3 minutes - adjusttable (120-300s is best)      

# Start the background loop when the app starts
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_bot())

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)



