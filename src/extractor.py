from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, quote

import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError

from .llm import LLMClient
from .utils import PageSnapshot, PlaywrightSession, ProductInfo, dump_debug_payload

logger = logging.getLogger(__name__)

_PRICE_REGEX = re.compile(r"([$€£¥]|USD|EUR|GBP|JPY|CAD|AUD)\s?[0-9.,]+", re.IGNORECASE)
_CURRENCY_MAP = {
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "¥": "JPY",
    "jpy": "JPY",
    "cad": "CAD",
    "aud": "AUD",
}

_SECONDARY_SECTION_MARKERS = [
    "best sellers",
    "customers also bought",
    "customers also viewed",
    "related items",
    "recommended for you",
    "people also bought",
    "similar items you might like",
    "sponsored products",
    "other items",
    "more deals",
    "deals our customers love best",
]


class ExtractionPipeline:
    def __init__(
        self,
        session: PlaywrightSession,
        llm_client: LLMClient,
        debug: bool = False,
        debug_dir: str = "debug-artifacts",
        site_profiles: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._session = session
        self._llm = llm_client
        self._debug = debug
        self._debug_dir = debug_dir
        self._http_timeout = aiohttp.ClientTimeout(total=45)
        self._site_profiles = site_profiles or {}
        self._headers_pool = [
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17 Safari/605.1.15",
                "Accept-Language": "en-US,en;q=0.8",
            },
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/126.0",
                "Accept-Language": "en-US,en;q=0.7",
            },
        ]

    def _trim_secondary_sections(self, content: Optional[str]) -> Optional[str]:
        if not content:
            return content
        lowered = content.lower()
        cutoff_index = None
        for marker in _SECONDARY_SECTION_MARKERS:
            idx = lowered.find(marker)
            if idx != -1 and idx > 200:  # ignore matches too early in the document
                cutoff_index = idx if cutoff_index is None else min(cutoff_index, idx)
        if cutoff_index is None:
            return content
        return content[:cutoff_index]

    async def extract_product_info(self, url: str, mavely_page=None) -> ProductInfo:
        logger.info("Starting product extraction for URL: %s", url)
        debug_payload: Dict[str, Any] = {"url": url, "stages": {}}

        logger.debug("Collecting browser snapshot...")
        snapshot = await self._collect_browser_snapshot(url, mavely_page=mavely_page)
        debug_payload["stages"]["browser"] = {
            "metadata": snapshot.metadata,
            "price_strings": snapshot.price_strings,
            "json_ld_count": len(snapshot.json_ld),
        }
        logger.info("Browser snapshot collected. Title from metadata: %s", snapshot.metadata.get("title"))
        logger.debug("Found %d JSON-LD blobs and %d price strings", len(snapshot.json_ld), len(snapshot.price_strings))

        product = self._from_snapshot(snapshot)
        logger.info("Initial extraction: title=%s, sale_price=%s, confidence=%.2f", 
                   product.title, product.sale_price, product.confidence)

        if self._needs_http_fallback(snapshot, product):
            logger.info("Attempting HTTP fallback for better extraction...")
            try:
                http_data = await asyncio.wait_for(self._http_fetch(url), timeout=15.0)  # Shorter timeout
                debug_payload["stages"]["http"] = {"status": http_data.get("status"), "reason": http_data.get("reason")}
                fallback_product = self._from_html(http_data.get("html", ""), http_data.get("text", ""))
                product = self._merge_products(product, fallback_product)
                logger.info("After HTTP fallback: title=%s, sale_price=%s", product.title, product.sale_price)
            except asyncio.TimeoutError:
                logger.warning("HTTP fallback timed out, skipping")
                debug_payload["stages"]["http"] = {"error": "timeout"}

        domain_product = await self._domain_specific_extraction(url)
        if domain_product:
            product = self._merge_products(product, domain_product)
            logger.info("Domain-specific extraction succeeded: title=%s, sale_price=%s", domain_product.title, domain_product.sale_price)

        if self._requires_llm(product):
            logger.info("Confidence too low (%.2f), invoking LLM for better extraction...", product.confidence)
            llm_result = await self._invoke_llm(snapshot, product)
            debug_payload["stages"]["llm"] = llm_result
            if llm_result:
                llm_product = ProductInfo(
                    title=llm_result.get("title"),
                    original_price=self._normalize_price(llm_result.get("original_price")),
                    sale_price=self._normalize_price(llm_result.get("sale_price")),
                    currency=self._infer_currency_from_candidates([
                        llm_result.get("original_price"),
                        llm_result.get("sale_price"),
                    ]),
                    confidence=0.3,
                    source="llm",
                )
                product = self._merge_products(product, llm_product)

        # If still no title, try to extract from URL
        if not product.title or product.title == "Access Denied":
            url_title = self._extract_title_from_url(url)
            if url_title:
                updated = asdict(product)
                updated['title'] = url_title
                updated['confidence'] = max(product.confidence, 0.5)
                product = ProductInfo(**updated)
                logger.info("Extracted title from URL: %s", url_title)

        if self._debug:
            debug_payload["final_product"] = asdict(product)
            try:
                dump_debug_payload(self._debug_dir, f"extract-{abs(hash(url))}", debug_payload)
            except Exception:  # pragma: no cover - best effort debug path
                logger.exception("Failed to write debug payload")

        return product

    def _extract_title_from_url(self, url: str) -> Optional[str]:
        """Extract product title from URL path."""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            path = parsed.path
            # For sites like macys.com/shop/product/title-here or homedepot.com/p/title-here
            if '/product/' in path:
                title_part = path.split('/product/')[-1].split('?')[0].split('/')[0]
            elif '/p/' in path:
                title_part = path.split('/p/')[-1].split('?')[0].split('/')[0]
            else:
                return None
            # Replace hyphens with spaces, capitalize words
            title = title_part.replace('-', ' ').title()
            # Clean up common words
            title = re.sub(r'\b(And|Or|The|A|An|In|On|At|To|For|Of|With|By)\b', lambda m: m.group().lower(), title)
            return title
        except Exception:
            pass
        return None

    async def _collect_browser_snapshot(self, url: str, mavely_page=None) -> PageSnapshot:
        # If we have a Mavely page (authenticated browser), use it to avoid bot detection
        browser = None
        if mavely_page:
            logger.info("Using Mavely's authenticated browser session to fetch product page")
            page = mavely_page
            context = page.context
            close_after = False
        else:
            logger.debug("Launching browser to scrape: %s", url)
            browser, context = await self._session.launch_transient_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            # Apply simple stealth tweaks
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            await context.add_init_script("window.chrome = {runtime: {}};")
            await context.add_init_script("Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});")
            await context.add_init_script("Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});")
            await context.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Dest": "document",
            })
            page = await context.new_page()
            close_after = True
        try:
            logger.debug("Navigating to URL...")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)  # Reduced timeout to 30s for faster response
            logger.debug("Waiting for page to fully load...")
            await asyncio.sleep(8.0)  # Increased wait time for dynamic content
            # Scroll to trigger lazy loading
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await asyncio.sleep(2.0)
            
            html = await page.content()
            text = await page.inner_text("body")
            page_title = await page.title()
            logger.info("Page loaded. Title: %s", page_title)
            logger.debug("Page text length: %d characters", len(text))
            logger.debug("First 500 chars of text: %s", text[:500])
            
            # Check if we're blocked
            is_blocked = "captcha" in html.lower() or "access denied" in html.lower() or "blocked" in html.lower() or "bot" in html.lower()
            if is_blocked:
                logger.error("⚠️ Page appears to be BLOCKED! Title: %s", page_title)
                logger.error("Text content: %s", text[:500])
            
            json_ld = await page.locator("script[type='application/ld+json']").all_text_contents()
            price_strings = _PRICE_REGEX.findall(html)
            logger.debug("Found %d price strings in HTML", len(price_strings))
            
            # Take screenshot for LLM vision, especially for blocked pages
            screenshot_path = None
            if self._debug or is_blocked:
                import pathlib
                debug_dir = pathlib.Path(self._debug_dir)
                debug_dir.mkdir(exist_ok=True)
                screenshot_path = str(debug_dir / f"screenshot_{hash(url)}.png")
                await page.screenshot(path=screenshot_path, full_page=True)
                logger.debug("Screenshot saved to %s", screenshot_path)
            
            metadata = {
                "page_title": page_title,
                "og_title": await self._read_meta(page, "property", "og:title"),
                "og_price": await self._read_meta(page, "property", "product:price:amount"),
                "og_currency": await self._read_meta(page, "property", "product:price:currency"),
                "blocked": is_blocked,
            }
            return PageSnapshot(url=url, html=html, text=text, json_ld=json_ld, price_strings=price_strings, metadata=metadata, screenshot_path=screenshot_path)
        except PlaywrightError as exc:
            logger.exception("Playwright failed to load %s: %s", url, exc)
            return PageSnapshot(url=url, html="", text="", json_ld=[], price_strings=[], metadata={"error": str(exc)}, screenshot_path=None)
        finally:
            if close_after:
                await context.close()
                await browser.close()
            else:
                # If using Mavely page, navigate back to Mavely home
                logger.debug("Navigating Mavely page back to home")
                try:
                    await page.goto("https://creators.mave.ly/home", wait_until="domcontentloaded", timeout=10000)
                except Exception as e:
                    logger.warning("Failed to navigate back to Mavely home: %s", e)

    async def _read_meta(self, page, attr: str, value: str) -> Optional[str]:
        locator = page.locator(f"meta[{attr}='{value}']")
        if await locator.count() == 0:
            return None
        return await locator.first.get_attribute("content")

    def _from_snapshot(self, snapshot: PageSnapshot) -> ProductInfo:
        product = ProductInfo(confidence=0.0, source="browser")

        profile_product = self._extract_from_profile(snapshot)
        product = profile_product or ProductInfo(confidence=0.0, source="profile")

        json_ld_product = self._extract_from_json_ld(snapshot.json_ld)
        if json_ld_product:
            product = self._merge_products(product, json_ld_product)

        dom_product = self._extract_from_dom(snapshot)
        product = self._merge_products(product, dom_product)

        if product.currency is None:
            currency = self._infer_currency_from_metadata(snapshot)
            if currency:
                product.currency = currency

        if product.confidence == 0.0 and (product.title or product.sale_price):
            product.confidence = 0.5

        return product

    def _extract_from_profile(self, snapshot: PageSnapshot) -> Optional[ProductInfo]:
        domain = urlparse(snapshot.url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        profile = self._site_profiles.get(domain)
        if not profile:
            return None

    def _extract_from_json_ld(self, json_ld_blobs: List[str]) -> Optional[ProductInfo]:
        for blob in json_ld_blobs:
            try:
                data = json.loads(blob)
            except json.JSONDecodeError:
                continue

            items = data if isinstance(data, list) else [data]
            for item in items:
                item_type = item.get("@type")
                if isinstance(item_type, list):
                    is_product = "Product" in item_type
                else:
                    is_product = item_type == "Product"
                if not is_product:
                    continue
                offers = item.get("offers")
                if isinstance(offers, list):
                    offer = offers[0]
                else:
                    offer = offers
                currency = None
                original_price = None
                sale_price = None
                if offer:
                    currency = offer.get("priceCurrency") or offer.get("priceCurrencyCode")
                    sale_price = offer.get("price") or offer.get("salePrice")
                    original_price = offer.get("highPrice") or offer.get("price")
                
                # Extract image URL from JSON-LD
                image_url = None
                image_data = item.get("image")
                if image_data:
                    if isinstance(image_data, str):
                        image_url = image_data
                    elif isinstance(image_data, list) and len(image_data) > 0:
                        image_url = image_data[0] if isinstance(image_data[0], str) else image_data[0].get("url")
                    elif isinstance(image_data, dict):
                        image_url = image_data.get("url")
                
                result = ProductInfo(
                    title=item.get("name"),
                    original_price=self._normalize_price(original_price),
                    sale_price=self._normalize_price(sale_price),
                    currency=currency,
                    image_url=image_url,
                    confidence=0.9,
                    source="json-ld",
                )
                return result
        return None

    def _extract_from_dom(self, snapshot: PageSnapshot) -> ProductInfo:
        primary_html = self._trim_secondary_sections(snapshot.html)
        primary_text = self._trim_secondary_sections(snapshot.text)

        soup = BeautifulSoup(snapshot.html, "html.parser")
        title_candidates = [
            snapshot.metadata.get("og_title"),
            soup.title.string if soup.title else None,
            getattr(soup.find("h1"), "get_text", lambda: None)(),
        ]
        title = next((t.strip() for t in title_candidates if t and t.strip()), None)

        price_candidates: List[str] = []
        seen_candidates: set[str] = set()

        def _add_candidate(raw: Optional[str]) -> None:
            if not raw:
                return
            candidate = raw.strip()
            if not candidate or candidate in seen_candidates:
                return
            seen_candidates.add(candidate)
            price_candidates.append(candidate)

        for value in snapshot.price_strings:
            if primary_html and value in primary_html:
                _add_candidate(value)
            elif primary_text and value in primary_text:
                _add_candidate(value)

        if primary_html:
            for value in _PRICE_REGEX.findall(primary_html):
                _add_candidate(value)
        if primary_text:
            for value in _PRICE_REGEX.findall(primary_text):
                _add_candidate(value)

        for meta_name in ["price", "sale_price"]:
            tag = soup.find("meta", attrs={"itemprop": meta_name})
            if tag and tag.get("content"):
                _add_candidate(tag["content"])

        sale_price, original_price = self._pick_prices(price_candidates)
        currency = snapshot.metadata.get("og_currency") or self._infer_currency_from_candidates(price_candidates)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("DOM price candidates (sample): %s", price_candidates[:10])
            logger.debug("Initial DOM prices: sale=%s original=%s", sale_price, original_price)

        def _find_discount_pair(content: Optional[str]) -> Optional[Tuple[str, str]]:
            if not content:
                return None
            match = re.search(
                r"\$?([0-9][\d,\.]*)(?:[\s\S]{0,120})\$?([0-9][\d,\.]*)(?:[\s\S]{0,60})%\s*off",
                content,
                re.IGNORECASE,
            )
            if not match:
                return None
            normalized_prices: List[str] = []
            for idx in (1, 2):
                normalized_val = self._normalize_price(match.group(idx))
                if normalized_val:
                    normalized_prices.append(normalized_val)
            if len(normalized_prices) != 2:
                return None
            try:
                ordered = sorted(normalized_prices, key=Decimal)
            except InvalidOperation:
                ordered = sorted(normalized_prices, key=lambda val: float(val))
            return ordered[0], ordered[-1]

        for content in (primary_html, primary_text):
            pair = _find_discount_pair(content)
            if pair:
                sale_candidate, original_candidate = pair
                if sale_candidate and not sale_price:
                    sale_price = sale_candidate
                if original_candidate and not original_price:
                    original_price = original_candidate
                if sale_price and original_price:
                    break

        collapsed_text = re.sub(r"\s+", " ", primary_text) if primary_text else ""
        if collapsed_text:
            was_now_match = re.search(
                r"Was\s+\$?([0-9][\d,\.]*)(?:[^$]{0,40})(?:Now|Today)\s*\$?([0-9][\d,\.]*)",
                collapsed_text,
                re.IGNORECASE,
            )
            if was_now_match:
                original_candidate = self._normalize_price(was_now_match.group(1))
                sale_candidate = self._normalize_price(was_now_match.group(2))
                if sale_candidate and not sale_price:
                    sale_price = sale_candidate
                if original_candidate and not original_price:
                    original_price = original_candidate

            if not original_price:
                reference_match = re.search(r"Reference Price\s*\$?([0-9][\d,\.]*)", collapsed_text, re.IGNORECASE)
                if reference_match:
                    original_candidate = self._normalize_price(reference_match.group(1))
                    if original_candidate:
                        original_price = original_candidate

        if title and not sale_price:
            title_prices = _PRICE_REGEX.findall(title)
            if title_prices:
                cleaned_prices: List[str] = []
                for price in title_prices:
                    num_match = re.search(r"[\d.,]+", price)
                    if num_match:
                        normalized_value = self._normalize_price(num_match.group())
                        if normalized_value:
                            cleaned_prices.append(normalized_value)
                if cleaned_prices:
                    sale_price = cleaned_prices[-1]
                    if len(cleaned_prices) > 1:
                        original_price = cleaned_prices[0]

        # Extract product image from DOM
        image_url = self._extract_product_image(soup, snapshot)

        return ProductInfo(
            title=title,
            original_price=original_price,
            sale_price=sale_price,
            currency=currency,
            image_url=image_url,
            confidence=0.6 if sale_price or original_price else 0.4,
            source="dom",
        )

    async def _http_fetch(self, url: str) -> Dict[str, Any]:
        headers = random.choice(self._headers_pool)
        try:
            async with aiohttp.ClientSession(timeout=self._http_timeout, headers=headers) as session:
                async with session.get(url, allow_redirects=True) as response:
                    html = await response.text()
                    text = self._strip_html(html)
                    return {
                        "status": response.status,
                        "reason": response.reason,
                        "html": html,
                        "text": text,
                    }
        except aiohttp.ClientError as exc:
            logger.exception("HTTP fallback failed for %s: %s", url, exc)
            return {"status": None, "reason": str(exc), "html": "", "text": ""}

    def _from_html(self, html: str, text: str) -> ProductInfo:
        if not html:
            return ProductInfo(confidence=0.0, source="http")

        primary_html = self._trim_secondary_sections(html)
        primary_text = self._trim_secondary_sections(text)

        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else None

        price_candidates: List[str] = []
        seen: set[str] = set()

        def _add_candidate(raw: Optional[str]) -> None:
            if not raw:
                return
            candidate = raw.strip()
            if not candidate or candidate in seen:
                return
            seen.add(candidate)
            price_candidates.append(candidate)

        if primary_html:
            for value in _PRICE_REGEX.findall(primary_html):
                _add_candidate(value)
        if primary_text:
            for value in _PRICE_REGEX.findall(primary_text):
                _add_candidate(value)

        sale_price, original_price = self._pick_prices(price_candidates)
        currency = self._infer_currency_from_candidates(price_candidates)

        return ProductInfo(
            title=title,
            original_price=original_price,
            sale_price=sale_price,
            currency=currency,
            confidence=0.45 if sale_price or original_price else 0.3,
            source="http",
        )

    async def _invoke_llm(self, snapshot: PageSnapshot, current: ProductInfo) -> Optional[Dict[str, Any]]:
        if not self._llm.enabled:
            logger.debug("LLM is not enabled, skipping")
            return None
        
        total_len = len(snapshot.text)
        chunk_len = max(500, min(4000, int(total_len * 0.25))) if total_len else 0
        text_to_send = snapshot.text[:chunk_len] if chunk_len else snapshot.text
        
        # If page is blocked or confidence is low, try to fetch full content via Jina AI and send that instead
        is_blocked = snapshot.metadata.get("blocked", False) or total_len < 200
        if is_blocked or current.confidence < 0.5:
            logger.info("Page blocked or confidence low, attempting to fetch full content via Jina AI")
            jina_text = await self._fetch_via_jina(snapshot.url)
            if jina_text and len(jina_text) > 500:  # Ensure it's substantial content
                text_to_send = jina_text
                logger.info("Fetched %d characters via Jina AI", len(text_to_send))
                # Extract prices from Jina text
                jina_price_strings = _PRICE_REGEX.findall(jina_text)
                snapshot.price_strings.extend(jina_price_strings)
                logger.info("Extracted %d price strings from Jina text: %s", len(jina_price_strings), jina_price_strings[:10])
            else:
                logger.warning("Jina AI fetch failed or insufficient content, using original text")
        else:
            logger.info("Using browser text for LLM (%d characters)", len(text_to_send))
        
        logger.debug("Text preview being sent to LLM: %s", text_to_send[:300])
        
        metadata = {
            "title": current.title,
            "currency": current.currency,
            "price_strings": snapshot.price_strings[:20],
            "url": snapshot.url,
            "blocked": is_blocked,
            "og_price": snapshot.metadata.get("og_price"),
            "og_currency": snapshot.metadata.get("og_currency"),
        }
        
        result = await self._llm.extract_product_fields(text_to_send, metadata, snapshot.screenshot_path if not is_blocked else None)
        if result:
            logger.info("LLM extracted: title=%s, original_price=%s, sale_price=%s", 
                       result.get("title"), result.get("original_price"), result.get("sale_price"))
        else:
            logger.warning("LLM returned no results")
        
        return result

    def _extract_price_patterns_from_url(self, url: str) -> List[str]:
        """Extract potential price patterns from URL, especially for sites like Macy's."""
        patterns = []
        # General price regex in URL
        price_matches = _PRICE_REGEX.findall(url)
        patterns.extend(price_matches)
        
        # Macy's specific: look for price in query params or path
        parsed = urlparse(url)
        if "macys.com" in parsed.netloc.lower():
            # Check query params for price hints
            query_params = parse_qs(parsed.query)
            for key, values in query_params.items():
                if "price" in key.lower():
                    patterns.extend(values)
            # Check path for price-like segments (e.g., price-79.99)
            path_parts = parsed.path.split('/')
            for part in path_parts:
                if re.search(r'\d+\.\d{2}', part):  # Matches X.XX format
                    patterns.append(part)
        
        return patterns

    async def _fetch_via_jina(self, url: str) -> Optional[str]:
        """Fetch page content via Jina AI for blocked pages (universal fallback)."""
        jina_url = f"https://r.jina.ai/{url}"
        headers = random.choice(self._headers_pool).copy()
        headers.update({
            "Accept": "text/plain",
        })
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(jina_url, headers=headers) as response:
                    if response.status == 200:
                        text = await response.text()
                        # Jina returns markdown-like text, extract the content part
                        lines = text.split('\n')
                        content_start = False
                        content_lines = []
                        for line in lines:
                            if line.startswith('Markdown Content:'):
                                content_start = True
                                continue
                            if content_start:
                                content_lines.append(line)
                        if content_lines:
                            return '\n'.join(content_lines).strip()
                        else:
                            return text  # Fallback to full text
                    else:
                        logger.warning("Jina AI request failed with status %s", response.status)
        except Exception as exc:
            logger.warning("Failed to fetch via Jina AI: %s", exc)
        return None

    def _merge_products(self, primary: ProductInfo, secondary: Optional[ProductInfo]) -> ProductInfo:
        if not secondary:
            return primary
        merged = ProductInfo(**primary.as_dict())
        
        # List of "bad" titles that should be replaced
        bad_titles = ["access denied", "denied", "blocked", "error", "forbidden"]
        
        for attr in ["title", "original_price", "sale_price", "currency", "image_url"]:
            primary_val = getattr(merged, attr)
            secondary_val = getattr(secondary, attr)
            
            # Replace if primary is empty/None
            if primary_val in (None, "") and secondary_val:
                setattr(merged, attr, secondary_val)
                merged.source = secondary.source or merged.source
                merged.confidence = max(merged.confidence, secondary.confidence)
            # Special case for title: replace bad titles with good ones
            elif attr == "title" and primary_val and secondary_val:
                if any(bad in primary_val.lower() for bad in bad_titles):
                    setattr(merged, attr, secondary_val)
                    merged.source = secondary.source or merged.source
                    merged.confidence = max(merged.confidence, secondary.confidence)
                    logger.info("Replaced bad title '%s' with '%s'", primary_val, secondary_val)
        
        merged.confidence = max(merged.confidence, secondary.confidence)
        return merged

    async def _domain_specific_extraction(self, url: str) -> Optional[ProductInfo]:
        # Use free proxy-based HTML fetching for any site
        return await self._extract_via_free_proxy(url)

    async def _extract_via_free_proxy(self, url: str) -> Optional[ProductInfo]:
        logger.info("Attempting universal extraction via free proxy for %s", url)
        proxy = await self._get_random_proxy()
        if not proxy:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, proxy=proxy, headers=random.choice(self._headers_pool), timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        return None
                    html = await response.text()
                    text = self._strip_html(html)
                    # Parse like HTTP fallback
                    return self._from_html(html, text)
        except Exception as exc:
            logger.warning("Free proxy extraction failed: %s", exc)
            return None

    async def _extract_macys(self, url: str) -> Optional[ProductInfo]:
        logger.info("Attempting Macy's domain-specific extraction")
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        product_ids = query_params.get("ID") or query_params.get("id")
        if not product_ids:
            logger.warning("No product ID found in Macy's URL")
            return None
        product_id = product_ids[0]
        api_url = f"https://www.macys.com/xapi/digital/v1/product/{product_id}"
        headers = random.choice(self._headers_pool).copy()
        headers.update({
            "Accept": "application/json, text/plain, */*",
            "Referer": url,
            "Origin": "https://www.macys.com",
        })
        data = None
        try:
            async with aiohttp.ClientSession(timeout=self._http_timeout) as session:
                async with session.get(api_url, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                    else:
                        logger.warning("Macy's API request failed with status %s", response.status)
        except Exception as exc:
            logger.warning("Failed to fetch Macy's API data: %s", exc)

        if data:
            product_data = data.get("product") or {}
            details = product_data.get("details", {})
            summary = product_data.get("summary", {})
            title = details.get("productName") or summary.get("name") or product_data.get("name")

            price_info = product_data.get("price") or {}
            sale_price, original_price, currency = self._parse_macys_price(price_info)
            
            # Extract image URL from Macy's API response
            image_url = None
            media = product_data.get("media") or product_data.get("imagery") or {}
            images = media.get("images") or []
            if images and len(images) > 0:
                # Try to get the primary or first image
                first_image = images[0]
                if isinstance(first_image, dict):
                    image_url = first_image.get("filePath") or first_image.get("url")
                    # Macy's often uses relative paths, make them absolute
                    if image_url and not image_url.startswith("http"):
                        image_url = f"https://slimages.macysassets.com/is/image/MCY/products/{image_url}"

            if any([title, sale_price, original_price]):
                return ProductInfo(
                    title=title,
                    original_price=original_price,
                    sale_price=sale_price,
                    currency=currency or "USD",
                    image_url=image_url,
                    confidence=0.85,
                    source="macys-api",
                )
            logger.warning("Macy's API did not return usable product data")

        proxy_product = await self._extract_macys_via_proxy(url)
        if proxy_product:
            return proxy_product

        return None

    def _parse_macys_price(self, price_info: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        sale_price = None
        original_price = None
        currency = None

        def extract_from_entry(entry: Any) -> Tuple[Optional[str], Optional[str]]:
            if isinstance(entry, dict):
                val = entry.get("value")
                formatted = entry.get("formatted")
                curr = entry.get("currencyCode") or entry.get("currency")
                value = None
                if val is not None:
                    try:
                        value = f"{Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"
                    except InvalidOperation:
                        logger.debug("Unable to parse Macy's numeric value: %s", val)
                elif formatted:
                    value = re.sub(r"[^0-9.]+", "", formatted)
                return value, curr
            elif isinstance(entry, (int, float)):
                try:
                    value = f"{Decimal(str(entry)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"
                except InvalidOperation:
                    logger.debug("Unable to quantize Macy's numeric value: %s", entry)
                    value = None
                return value, None
            elif isinstance(entry, str):
                value = re.sub(r"[^0-9.]+", "", entry)
                return value or None, None
            return None, None

        if isinstance(price_info, dict):
            for key, value in price_info.items():
                extracted_value, extracted_currency = extract_from_entry(value)
                if not extracted_value:
                    continue
                lowered = key.lower()
                if any(token in lowered for token in ["sale", "current", "offer", "promo", "now"]):
                    sale_price = sale_price or extracted_value
                if any(token in lowered for token in ["regular", "original", "list", "was"]):
                    original_price = original_price or extracted_value
                if not currency and extracted_currency:
                    currency = extracted_currency

        return sale_price, original_price, currency

    async def _extract_macys_via_proxy(self, url: str) -> Optional[ProductInfo]:
        proxy_url = f"https://r.jina.ai/{url}"
        headers = random.choice(self._headers_pool).copy()
        headers.update({
            "Accept": "text/plain",
        })
        try:
            async with aiohttp.ClientSession(timeout=self._http_timeout) as session:
                async with session.get(proxy_url, headers=headers) as response:
                    if response.status != 200:
                        logger.warning("Macy's proxy fetch failed with status %s", response.status)
                        return None
                    proxy_text = await response.text()
        except Exception as exc:
            logger.warning("Failed to fetch Macy's proxy content: %s", exc)
            return None

        if not proxy_text:
            return None

        # Restrict to the main product section to avoid cross-product price bleed
        main_section = proxy_text.split("Shop similar styles", 1)[0]
        title_match = re.search(r"Title:\s*(.+)", proxy_text)
        title = title_match.group(1).strip() if title_match else None
        if title:
            title = title.replace(" - Macy's", "").strip()

        price_matches = _PRICE_REGEX.findall(main_section)
        normalized_prices = [self._normalize_price(match) for match in price_matches]
        normalized_prices = [price for price in normalized_prices if price]

        if not normalized_prices:
            logger.warning("Macy's proxy content did not contain recognizable prices")
            return ProductInfo(
                title=title,
                confidence=0.5,
                source="macys-proxy",
            )

        sale_price = normalized_prices[0]
        original_price = next((price for price in normalized_prices[1:] if price != sale_price), None)

        currency = self._infer_currency_from_candidates(price_matches) or "USD"

        return ProductInfo(
            title=title,
            original_price=original_price,
            sale_price=sale_price,
            currency=currency,
            confidence=0.75,
            source="macys-proxy",
        )

    def _pick_prices(self, candidates: List[str]) -> Tuple[Optional[str], Optional[str]]:
        if not candidates:
            return None, None
        normalized = []
        for candidate in candidates:
            normalized_price = self._normalize_price(candidate)
            if normalized_price:
                normalized.append((candidate, normalized_price))
        if not normalized:
            return None, None
        try:
            ordered_values = sorted({item[1] for item in normalized}, key=Decimal)
        except InvalidOperation:
            ordered_values = sorted({item[1] for item in normalized}, key=lambda value: float(value))

        best_sale = None
        best_original = None
        best_discount = Decimal("-1")

        for i, sale_str in enumerate(ordered_values):
            try:
                sale_val = Decimal(sale_str)
            except InvalidOperation:
                continue
            if sale_val <= 0:
                continue
            for original_str in ordered_values[i + 1:]:
                try:
                    original_val = Decimal(original_str)
                except InvalidOperation:
                    continue
                if original_val <= 0 or sale_val >= original_val:
                    continue
                discount = (original_val - sale_val) / original_val
                if discount > best_discount:
                    best_discount = discount
                    best_sale = sale_str
                    best_original = original_str

        if best_sale and best_original:
            return best_sale, best_original

        lowest = ordered_values[0]
        highest = ordered_values[-1]
        return lowest, highest if highest != lowest else None

    def _normalize_price(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        stripped = re.sub(r"[^0-9.,]", "", value)
        stripped = stripped.replace(",", "")
        if not stripped:
            return None
        try:
            numeric = Decimal(stripped)
        except InvalidOperation:
            return None
        quantized = numeric.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{quantized:.2f}"

    def _infer_currency_from_metadata(self, snapshot: PageSnapshot) -> Optional[str]:
        candidates = [snapshot.metadata.get("og_currency")]
        return self._infer_currency_from_candidates(candidates + snapshot.price_strings)

    def _infer_currency_from_candidates(self, candidates: List[Optional[str]]) -> Optional[str]:
        for candidate in candidates:
            if not candidate:
                continue
            for key, code in _CURRENCY_MAP.items():
                if key.lower() in str(candidate).lower():
                    return code
        return None

    def _strip_html(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator=" ")

    def _query_first(self, soup: BeautifulSoup, selector_spec: Any) -> Optional[str]:
        if not selector_spec:
            return None
        selectors: List[str]
        if isinstance(selector_spec, str):
            selectors = [selector_spec]
        elif isinstance(selector_spec, list):
            selectors = selector_spec
        else:
            return None
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                if element.has_attr("content"):
                    return element["content"].strip()
                text = element.get_text(strip=True)
                if text:
                    return text
        return None

    def _extract_product_image(self, soup: BeautifulSoup, snapshot: PageSnapshot) -> Optional[str]:
        """Extract the main product image URL from the page."""
        logger.debug("Starting image extraction for %s", snapshot.url)
        
        # Try Open Graph image first
        og_image = snapshot.metadata.get("og_image")
        if og_image and isinstance(og_image, str) and og_image.startswith("http"):
            logger.info("Found product image via Open Graph: %s", og_image[:100])
            return og_image
        
        # Try common product image selectors
        image_selectors = [
            'meta[property="og:image"]',
            'meta[name="og:image"]',
            'img[itemprop="image"]',
            'img.product-image',
            'img.product-img',
            'img#product-image',
            'img[data-testid*="product-image"]',
            'img[class*="ProductImage"]',
            'img[class*="product-image"]',
            'img[alt*="product"]',
            'div.product-media img',
            'div.product-gallery img',
            'div[class*="ProductImage"] img',
            'div[class*="product-image"] img',
        ]
        
        for selector in image_selectors:
            try:
                if selector.startswith('meta'):
                    meta = soup.select_one(selector)
                    if meta and meta.get("content"):
                        img_url = meta["content"]
                        if img_url.startswith("http"):
                            logger.info("Found product image via selector %s: %s", selector, img_url[:100])
                            return img_url
                else:
                    img = soup.select_one(selector)
                    if img:
                        img_url = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                        if img_url:
                            # Handle relative URLs
                            if img_url.startswith("//"):
                                img_url = "https:" + img_url
                            elif img_url.startswith("/"):
                                from urllib.parse import urlparse
                                parsed = urlparse(snapshot.url)
                                img_url = f"{parsed.scheme}://{parsed.netloc}{img_url}"
                            if img_url.startswith("http"):
                                logger.info("Found product image via selector %s: %s", selector, img_url[:100])
                                return img_url
            except Exception as e:
                logger.debug("Error extracting image with selector %s: %s", selector, e)
                continue
        
        # Fallback: find the first large image on the page
        try:
            all_images = soup.find_all("img", limit=20)
            for img in all_images:
                img_url = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                if not img_url:
                    continue
                
                # Skip small images, icons, logos
                width = img.get("width")
                height = img.get("height")
                if width and height:
                    try:
                        if int(width) < 200 or int(height) < 200:
                            continue
                    except (ValueError, TypeError):
                        pass
                
                # Skip common non-product images
                skip_patterns = ["logo", "icon", "banner", "badge", "sprite"]
                if any(pattern in img_url.lower() for pattern in skip_patterns):
                    continue
                
                # Handle relative URLs
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                elif img_url.startswith("/"):
                    from urllib.parse import urlparse
                    parsed = urlparse(snapshot.url)
                    img_url = f"{parsed.scheme}://{parsed.netloc}{img_url}"
                
                if img_url.startswith("http"):
                    logger.info("Found product image via fallback (large image): %s", img_url[:100])
                    return img_url
        except Exception as e:
            logger.debug("Error in fallback image extraction: %s", e)
        
        logger.warning("No product image found for %s", snapshot.url)
        return None

    def _needs_http_fallback(self, snapshot: PageSnapshot, product: ProductInfo) -> bool:
        if snapshot.metadata.get("blocked") or len(snapshot.html) < 2000:
            return True
        if product.title and (product.sale_price or product.original_price):
            return False
        return True

    def _requires_llm(self, product: ProductInfo) -> bool:
        if not self._llm.enabled:
            return False
        return not (product.title and (product.sale_price or product.original_price))

    async def _get_random_proxy(self) -> Optional[str]:
        # Fetch from a free proxy list (e.g., https://free-proxy-list.net/)
        # In production, use a paid service for reliability
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://free-proxy-list.net/', headers=random.choice(self._headers_pool)) as response:
                    if response.status == 200:
                        soup = BeautifulSoup(await response.text(), 'html.parser')
                        proxies = []
                        for row in soup.find_all('tr')[1:]:  # Skip header
                            cols = row.find_all('td')
                            if len(cols) > 1 and cols[6].text == 'yes':  # HTTPS
                                proxies.append(f"http://{cols[0].text}:{cols[1].text}")
                        return random.choice(proxies) if proxies else None
        except Exception:
            pass
        return None
