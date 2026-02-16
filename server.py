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
            list_url = "https://api.coingecko.com/api/v3/coins/list?include_platform=true"
            try:
                async with session.get(list_url) as resp:
                    if resp.status != 200:
                        logging.error(f"CoinGecko list failed: {resp.status}")
                        return
                    all_coins = await resp.json()
                    logging.info(f"Received {len(all_coins)} total coins from /list")
            except Exception as e:
                logging.error(f"List fetch error: {e}")
                return

            # Filter Solana tokens
            sol_tokens = []
            for coin in all_coins:
                sol_addr = coin.get('platforms', {}).get('solana')
                if sol_addr and isinstance(sol_addr, str) and len(sol_addr) > 30:
                    sol_tokens.append({
                        'id': coin['id'],
                        'symbol': coin['symbol'].upper(),
                        'address': sol_addr
                    })

            logging.info(f"Found {len(sol_tokens)} potential Solana tokens")

            if len(sol_tokens) == 0:
                logging.warning("No Solana tokens found - check network/API")
                return

            # Step 2: Get market cap for first 250 with retry on 429
            ids = ','.join(t['id'] for t in sol_tokens[:250])
            markets_url = "https://api.coingecko.com/api/v3/coins/markets"
            params = {
                'vs_currency': 'usd',
                'ids': ids,
                'order': 'market_cap_desc',
                'per_page': 250,
                'page': 1,
            }

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    async with session.get(markets_url, params=params) as resp:
                        if resp.status == 429:
                            wait_time = 60  # seconds
                            logging.warning(f"Rate limit hit (429) - waiting {wait_time}s before retry {attempt+1}/{max_retries}")
                            await asyncio.sleep(wait_time)
                            continue
                        if resp.status == 200:
                            data = await resp.json()
                            self.tokens = {}
                            for market in data:
                                market_cap = market.get('market_cap')
                                if market_cap is None:
                                    market_cap = 0  # safe default
                                if market_cap > 1_000_000:
                                    symbol = market['symbol'].upper()
                                    for t in sol_tokens:
                                        if t['id'] == market['id']:
                                            self.tokens[symbol] = {'address': t['address']}
                                            break
                            logging.info(f"Loaded {len(self.tokens)} Solana tokens > $1M MC")
                            return  # success - exit
                        else:
                            logging.error(f"Markets fetch failed: {resp.status}")
                            return
                except Exception as e:
                    logging.error(f"Markets fetch error on attempt {attempt+1}: {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(30)
                    else:
                        return

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
        for symbol, info in self.tokens.items():
            address = info['address']
        
            ds_price = await self.get_dexscreener_price(session, address)
            if ds_price is None:
                continue

            # For now, use DexScreener as baseline (Jupiter DNS issue)
            # Later: re-add Jupiter price when fixed
            baseline_price = ds_price

            # Placeholder diff (0% since only one source) - will be real when Jupiter works
            diff_pct = 0.0  # Update this when we have two prices
            logging.info(f"{symbol}: DS ${ds_price:.6f} (baseline)")

            # Example detection (later: real diff check)
            logging.info(f"Scanning {symbol} - address {address} - price ${ds_price:.6f}")
            # If we had two prices:
            # if diff_pct > 0.5:
            #     logging.warning(f"Opportunity on {symbol}! Diff {diff_pct:.2f}%")
            #     # Call attempt_roundtrip_arb here when ready

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
    while True:
        if bot.running:
            if not bot.tokens:
                await bot.fetch_solana_tokens()
            logging.info(f"Bot is running with {len(bot.tokens)} tokens")
            async with aiohttp.ClientSession() as session:
                await bot.scan_for_opportunities(session)
        await asyncio.sleep(60)

# Start the background loop when the app starts
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_bot())

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)



