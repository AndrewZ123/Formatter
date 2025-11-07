from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import discord
from discord import app_commands

from .extractor import ExtractionPipeline
from .llm import LLMClient
from .mavely import MavelyLinkService
from .utils import (
    PlaywrightSession,
    configure_logging,
    load_settings,
    load_site_profiles,
)

logger = logging.getLogger(__name__)


class AffiliateBot(discord.Client):
    COMMAND_PATTERN = re.compile(r"^[!/](?:format)\s+(https?://\S+)\s+([\d.]+)\s+([\d.]+)", re.IGNORECASE)

    def __init__(
        self,
        channel_id: int,
        extractor: ExtractionPipeline,
        mavely: MavelyLinkService,
        llm: LLMClient,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self._channel_id = channel_id
        self._extractor = extractor
        self._mavely = mavely
        self._llm = llm
        self._semaphore = asyncio.Semaphore(2)
        self.tree = app_commands.CommandTree(self)
        self._slash_registered_guild: Optional[int] = None
        self._setup_commands()

    def _setup_commands(self) -> None:
        """Register slash commands with the command tree."""
        @app_commands.command(name="format", description="Format a product link with discount info")
        @app_commands.describe(
            url="Product URL (not a Mavely link)",
            was_price="Original price (e.g. 29.99)",
            now_price="Sale price (e.g. 19.99)",
        )
        async def format_cmd(
            interaction: discord.Interaction,
            url: str,
            was_price: app_commands.Range[float, 0, 1_000_000],
            now_price: app_commands.Range[float, 0, 1_000_000],
        ) -> None:
            if interaction.channel_id != self._channel_id:
                await interaction.response.send_message(
                    "Use this command in the designated channel.",
                    ephemeral=True,
                )
                return

            if "mavely.app.link" in url or "joinmavely" in url:
                await interaction.response.send_message("Please provide a non-Mavely product link.", ephemeral=True)
                return

            await interaction.response.defer()

            async with self._semaphore:
                try:
                    reply_text, image_url = await self._handle_format(url, was_price, now_price)
                    
                    # Send text first, then image as separate message if available
                    await interaction.followup.send(reply_text)
                    if image_url:
                        await interaction.channel.send(image_url)
                except Exception as exc:  # pragma: no cover
                    logger.exception("Error processing slash /format for %s: %s", url, exc)
                    await interaction.followup.send("⚠️ Something went wrong while processing that link. Please try again.")

        self._format_command = format_cmd

    async def setup_hook(self) -> None:
        await self._mavely.start()
        await self._llm.start()
        # Prepare slash command registration for the target guild
        try:
            channel = self.get_channel(self._channel_id) or await self.fetch_channel(self._channel_id)
        except discord.HTTPException as exc:
            logger.error("Unable to locate channel %s for slash command sync: %s", self._channel_id, exc)
            return

        guild = getattr(channel, "guild", None)
        if not guild:
            logger.warning("Channel %s has no guild; skipping slash command registration", self._channel_id)
            return

        # First, clear global commands to make sure no stale definitions linger.
        self.tree.clear_commands(guild=None)
        try:
            await self.tree.sync()
            logger.info("Cleared global slash commands during sync")
        except app_commands.CommandSyncFailure as exc:
            logger.warning("Global slash command sync failed: %s", exc)
        except discord.HTTPException as exc:
            logger.warning("HTTP error while clearing global slash commands: %s", exc)

        # Now register the guild-scoped /format command freshly to avoid signature mismatches.
        self.tree.clear_commands(guild=guild)
        try:
            self.tree.add_command(self._format_command, guild=guild, override=True)
        except app_commands.CommandAlreadyRegistered:
            self.tree.remove_command(self._format_command.name, guild=guild)
            self.tree.add_command(self._format_command, guild=guild)

        try:
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d slash command(s) to guild %s", len(synced), guild.id)
            self._slash_registered_guild = guild.id
        except discord.HTTPException as exc:
            logger.error("Failed to sync slash commands to guild %s: %s", guild.id, exc)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.author == self.user:
            return
        if message.channel.id != self._channel_id:
            return
            

        match = self.COMMAND_PATTERN.match(message.content.strip())
        if not match:
            return

        url, was_raw, now_raw = match.groups()
        if "mavely.app.link" in url or "joinmavely" in url:
            await message.reply("Please provide a non-Mavely product link.")
            return

        try:
            was_price = float(was_raw)
            now_price = float(now_raw)
        except ValueError:
            await message.reply("Could not parse the prices. Use `/format <url> <was_price> <now_price>` like `/format https://example.com 29.99 19.99`." )
            return

        asyncio.create_task(self._process_request(message, url, was_price, now_price))

    async def _process_request(self, message: discord.Message, url: str, was_price: float, now_price: float) -> None:
        async with self._semaphore:
            reaction_added = False
            try:
                await message.add_reaction("⏳")
                reaction_added = True
            except discord.HTTPException:
                pass

            try:
                reply_text, image_url = await self._handle_format(url, was_price, now_price)
                
                # Send text first, then image as separate message if available
                await message.reply(reply_text, mention_author=False)
                if image_url:
                    await message.channel.send(image_url)
            except Exception as exc:  # pragma: no cover
                logger.exception("Error processing format command for %s: %s", url, exc)
                await message.reply("⚠️ Something went wrong while processing that link. Please try again.")
            finally:
                if reaction_added:
                    try:
                        await message.remove_reaction("⏳", self.user)  # type: ignore[arg-type]
                    except discord.HTTPException:
                        pass

    async def _handle_format(self, url: str, was_price: float, now_price: float) -> tuple[str, Optional[str]]:
        # 1) Create affiliate link
        affiliate_link = await self._mavely.create_mavely_link(url)

        # 2) Extract title and image with timeout; fallback to URL-based title
        title: Optional[str] = None
        image_url: Optional[str] = None
        product = None
        
        try:
            mavely_page = self._mavely.get_page()
            product = await asyncio.wait_for(
                self._extractor.extract_product_info(url, mavely_page=mavely_page),
                timeout=20.0,
            )
            if product:
                title = (product.title or "").strip()
                image_url = product.image_url
                logger.info("Extracted product - title: %s, image: %s", title[:50] if title else None, image_url[:80] if image_url else None)
        except asyncio.TimeoutError:
            logger.warning("Product extraction timed out after 20s")
        except Exception as e:
            logger.warning("Product extraction failed: %s", e, exc_info=True)

        if not title or title.lower() in {"access denied", "denied", "blocked"}:
            title = self._simple_title_from_url(url)
            logger.info("Using URL fallback title: %s", title)

        reply_text = self._format_reply(affiliate_link, title, was_price, now_price)
        
        if image_url:
            logger.info("Including product image in response: %s", image_url[:100])
        else:
            logger.warning("No product image found for %s", url)
        
        return reply_text, image_url

    def _simple_title_from_url(self, url: str) -> str:
        """Extract a clean product title from the URL path."""
        from urllib.parse import urlparse, unquote
        
        parsed = urlparse(url)
        path = unquote(parsed.path)
        
        # Special handling for common e-commerce patterns
        # Match patterns like /shop/product/product-name or /product/product-name
        patterns = [
            r'/(?:shop/)?product/([^/?]+)',  # Macy's, Amazon, etc.
            r'/offers/([^/?]+)',              # Woot
            r'/p/([^/?]+)',                   # Target, Walmart
            r'/dp/([^/?]+)',                  # Amazon
            r'/item/([^/?]+)',                # eBay
        ]
        
        slug = None
        for pattern in patterns:
            match = re.search(pattern, path, re.IGNORECASE)
            if match:
                slug = match.group(1)
                break
        
        # Fallback: use last path segment
        if not slug:
            m = re.search(r'/([^/?#]+)/?$', path)
            slug = m.group(1) if m else path
        
        # Clean up the slug
        slug = slug.replace('-', ' ').replace('_', ' ')
        
        # Remove common variant fluff
        slug = re.sub(r'\b(pack|ct|count|size|set)\s+of\s+\d+\b', '', slug, flags=re.IGNORECASE)
        slug = re.sub(r'\b\d+(\.\d+)?\s*(in|inch|inches|cm|mm|oz|fl oz|lb|lbs)\b', '', slug, flags=re.IGNORECASE)
        slug = re.sub(r'\b(sku|model)\s*[:#]?\s*\w+\b', '', slug, flags=re.IGNORECASE)
        slug = re.sub(r'\b(black|white|red|blue|green|yellow|orange|purple|pink|grey|gray|silver|gold|bronze)\b', '', slug, flags=re.IGNORECASE)
        
        # Remove IDs and query-like patterns that snuck into path
        slug = re.sub(r'\bid\s*=?\s*\d+\b', '', slug, flags=re.IGNORECASE)
        slug = re.sub(r'\btdp\s*=.*$', '', slug, flags=re.IGNORECASE)
        
        # Clean up whitespace
        slug = re.sub(r'\s+', ' ', slug).strip(' -_')
        
        if not slug or len(slug) < 3:
            slug = "Product"
        
        return slug.title()

    def _format_reply(self, affiliate_link: str, title: str, was_price: float, now_price: float) -> str:
        # Clean marketing fragments from title
        t = re.sub(r'\s*-\s*\$[\d.,]+\s*-\s*', ' ', title)
        t = re.sub(r'\s*Free shipping for Prime members\s*', '', t, flags=re.IGNORECASE).strip(' -')
        title_upper = (t or "Product").upper()

        discount_percent = None
        if was_price > 0 and 0 <= now_price <= was_price:
            discount_percent = round((was_price - now_price) / was_price * 100)

        if discount_percent and discount_percent > 0:
            title_line = f"GET {discount_percent}% OFF {title_upper}"
        else:
            title_line = title_upper

        was_s = f"${was_price:0.2f}"
        now_s = f"${now_price:0.2f}"
        price_line = f"Was: {was_s} → **Now: {now_s}**"

        return f"**{title_line}**\n\n{price_line}\n\n🔗 <{affiliate_link}>"


def run_bot() -> None:
    settings = load_settings()
    configure_logging(settings.debug_extract)

    session = PlaywrightSession(settings.headful)
    llm_client = LLMClient(settings.openai_api_key, settings.openai_model)
    site_profiles = load_site_profiles()
    extractor = ExtractionPipeline(
        session=session,
        llm_client=llm_client,
        debug=settings.debug_extract,
        debug_dir=settings.debug_dir,
        site_profiles=site_profiles,
    )
    mavely = MavelyLinkService(
        session=session,
        email=settings.mavely_email,
        password=settings.mavely_password,
    )

    client = AffiliateBot(
        channel_id=settings.channel_id,
        extractor=extractor,
        mavely=mavely,
        llm=llm_client,
    )

    async def runner() -> None:
        try:
            async with client:
                await client.start(settings.discord_token)
        finally:
            await mavely.stop()
            await session.stop()
            await llm_client.close()

    asyncio.run(runner())


if __name__ == "__main__":
    run_bot()
