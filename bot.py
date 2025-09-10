# bot.py
import os, io
from pathlib import Path

# Make Playwright look for browsers in a local cache dir (optional).
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path(__file__).resolve().parent / ".playwright-cache")
)

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
        # Start Playwright inside Discord's loop (headless)
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self.playwright:
                await self.playwright.stop()
        await super().close()

intents = discord.Intents.default()
intents.message_content = True
bot = HeadlessBot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

# ---------------------------
# Helpers
# ---------------------------

async def _load_and_settle(page, url: str):
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_load_state("networkidle", timeout=30_000)
    await page.wait_for_timeout(250)

async def _dismiss_blockers(page):
    # Close welcome / tutorial / modal overlays if present
    try:
        close_btn = page.get_by_role("button", name="Close")
        if await close_btn.is_visible():
            await close_btn.click()
            await page.wait_for_timeout(100)
    except Exception:
        pass

async def _open_shop(page, url: str):
    await _load_and_settle(page, url)
    await _dismiss_blockers(page)

    # Click the bottom bar "Shop" button
    shop_btn = page.get_by_role("button", name="Shop")
    await shop_btn.click()

    # Wait for shop container/cards to appear
    # Main container seen in your HTML dump is `.css-brvep9`
    await page.wait_for_selector(
        ".css-brvep9, button.chakra-button:has-text('Seed'), button.chakra-button:has-text('Cutting')",
        timeout=20_000,
    )
    await page.wait_for_timeout(200)

def _chunk_or_file(text: str, fname="shop.html"):
    # Discord messages max ~2000 chars. Attach file if bigger.
    if len(text) <= 1900:
        return {"content": f"```html\n{text}\n```"}
    return {"file": discord.File(io.BytesIO(text.encode("utf-8")), filename=fname)}

# ---------------------------
# Commands
# ---------------------------

@bot.command(name="fetch_shop_html")
async def fetch_shop_html(ctx, url: str | None = None):
    """
    Click 'Shop' then send the shop container HTML (or the whole page if not found).
    Usage: !fetch_shop_html [optional_url]
    """
    url = url or DEFAULT_URL
    if not (url.startswith("http://") or url.startswith("https://")):
        await ctx.send("Please provide a valid http(s) URL.")
        return

    page = await bot.browser.new_page()
    try:
        await _open_shop(page, url)
        html = await page.evaluate("""
            () => {
              const el = document.querySelector('.css-brvep9');
              return el ? el.outerHTML : document.documentElement.outerHTML;
            }
        """)
        payload = _chunk_or_file(html, fname="shop.html")
        await ctx.send(**payload)
    except PWTimeout:
        await ctx.send("Timed out opening the Shop.")
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        await page.close()

@bot.command(name="shop_items")
async def shop_items(ctx, url: str | None = None):
    """
    Click 'Shop', parse item name + stock + price text + rarity, and print them.
    Usage: !shop_items [optional_url]
    """
    url = url or DEFAULT_URL
    if not (url.startswith("http://") or url.startswith("https://")):
        await ctx.send("Please provide a valid http(s) URL.")
        return

    page = await bot.browser.new_page()
    try:
        await _open_shop(page, url)

        data = await page.evaluate("""() => {
          // Find all product cards (each is a <button class="chakra-button ...">)
          const container = document.querySelector('.css-brvep9') || document;
          const cards = Array.from(container.querySelectorAll('button.chakra-button'));
          const rows = [];

          for (const card of cards) {
            const name = card.querySelector('p.chakra-text.css-swfl2y')?.textContent?.trim();
            if (!name) continue;

            // Stock text area
            const stockNode = card.querySelector('.McFlex.css-n6egec p.chakra-text') ||
                              card.querySelector('.McFlex.css-n6egec .chakra-text');
            const stockTextRaw = stockNode?.textContent?.trim() || '';

            let stock = 0;
            let stockStatus = 'UNKNOWN';
            const m = stockTextRaw.match(/X\\s*(\\d+)\\s*Stock/i);
            if (m) {
              stock = parseInt(m[1], 10);
              stockStatus = 'IN_STOCK';
            } else if (/NO\\s+LOCAL\\s+STOCK/i.test(stockTextRaw) || /NO\\s+STOCK/i.test(stockTextRaw)) {
              stock = 0;
              stockStatus = 'NO_STOCK';
            }

            // Price text (as displayed, e.g., "1,300", "1M", "2.75M")
            const priceText = (card.querySelector('.css-g4qgtg')?.textContent || '').trim();

            // Rarity (various wrappers/classes)
            const rarity = (
              card.querySelector('.css-1ibz8bl, .css-10hyd36, .css-njp13n')?.textContent || ''
            ).trim() || null;

            // "WEB EXCLUSIVE" badge?
            const webExclusive = !!card.querySelector('.css-3ajrt9, .css-j2e9cr');

            rows.push({ name, stock, stockText: stockTextRaw, stockStatus, priceText, rarity, webExclusive });
          }
          return rows;
        }""")

        if not data:
            await ctx.send("No items found in the Shop.")
            return

        # Format nicely; attach as a text file if it gets long
        lines = []
        for d in data:
            badge = " (WEB EXCLUSIVE)" if d.get("webExclusive") else ""
            rarity = f" — {d['rarity']}" if d.get("rarity") else ""
            price = f" — {d['priceText']}" if d.get("priceText") else ""
            stock = f"{d['stock']} in stock" if d.get("stockStatus") == "IN_STOCK" else d.get("stockText", "Unknown")
            lines.append(f"- **{d['name']}**{badge}{rarity}{price} — {stock}")

        output = "\n".join(lines)
        if len(output) <= 1900:
            await ctx.send(output)
        else:
            await ctx.send(file=discord.File(io.BytesIO(output.encode("utf-8")), filename="shop_items.txt"))

    except PWTimeout:
        await ctx.send("Timed out opening the Shop.")
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        await page.close()

@bot.command()
async def fetch_html(ctx, url: str | None = None):
    """Render the page (no click) and send the full HTML. Usage: !fetch_html [optional_url]"""
    url = url or DEFAULT_URL
    if not (url.startswith("http://") or url.startswith("https://")):
        await ctx.send("Please provide a valid http(s) URL.")
        return

    page = await bot.browser.new_page()
    try:
        await _load_and_settle(page, url)
        html = await page.content()
        payload = _chunk_or_file(html, fname="page.html")
        await ctx.send(**payload)
    except PWTimeout:
        await ctx.send("Timed out loading the page.")
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        await page.close()

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN in your .env")
bot.run(DISCORD_TOKEN)
