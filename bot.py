# bot.py
import os
import io
from pathlib import Path

# Ensure Playwright looks in the project's cache before we import it or start it.
# This must be set early so the Playwright driver picks up the correct browsers path.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parent / ".playwright-cache"))

import discord
from discord.ext import commands
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_URL = os.getenv("TARGET_URL", "https://magiccircle.gg/r/LDQK")

class HeadlessBot(commands.Bot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.playwright = None
        self.browser = None

    async def setup_hook(self):
        # Start Playwright inside Discord's event loop (headless)
        # We rely on PLAYWRIGHT_BROWSERS_PATH env var above so Playwright finds the installed browsers.
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )

    async def close(self):
        # Clean shutdown
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self.playwright:
                await self.playwright.stop()
        await super().close()

intents = discord.Intents.default()
intents.message_content = True  # make sure this intent is enabled in Dev Portal
bot = HeadlessBot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

async def get_rendered_html(url: str) -> str:
    page = await bot.browser.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_load_state("networkidle", timeout=30_000)
        await page.wait_for_timeout(300)  # small buffer for SPA paint
        return await page.content()
    finally:
        await page.close()

@bot.command(name="fetch_html")
async def fetch_html(ctx, url: str | None = None):
    """Render the page and send its HTML. Usage: !fetch_html [optional_url]"""
    url = url or DEFAULT_URL
    if not (url.startswith("http://") or url.startswith("https://")):
        await ctx.send("Please provide a valid http(s) URL.")
        return
    try:
        html = await get_rendered_html(url)
        if len(html) <= 1900:
            await ctx.send(f"```html\n{html}\n```")
        else:
            await ctx.send(file=discord.File(io.BytesIO(html.encode("utf-8")), filename="page.html"))
    except PWTimeout:
        await ctx.send("Timed out loading the page.")
    except Exception as e:
        await ctx.send(f"Error: {e}")

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN in your .env")
bot.run(DISCORD_TOKEN)
