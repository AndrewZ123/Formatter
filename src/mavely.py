from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import random
from typing import Optional

from playwright.async_api import BrowserContext, Error as PlaywrightError

from .utils import PlaywrightSession

logger = logging.getLogger(__name__)


class MavelyAutomationError(RuntimeError):
    pass


class MavelyLinkService:
    """Automates the Mavely creator dashboard to mint affiliate links."""

    HOME_URL = "https://creators.mave.ly/home"
    LOGIN_URL = "https://creators.mave.ly/login"

    def __init__(
        self,
        session: PlaywrightSession,
        email: str,
        password: str,
        profile_dir: str = ".mavely-profile",
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._profile_dir = str(pathlib.Path(profile_dir).resolve())
        self._context: Optional[BrowserContext] = None
        self._page = None  # Keep one page open for the lifetime of the service
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._context:
            return
        self._context = await self._session.launch_persistent_context(
            user_data_dir=self._profile_dir,
            viewport={"width": 1280, "height": 720},
        )
        await self._ensure_logged_in()

    def get_page(self):
        """Get the persistent Mavely page for use in product extraction."""
        return self._page

    async def stop(self) -> None:
        if self._page:
            try:
                await self._page.close()
            except Exception as exc:
                logger.warning("Failed to close page: %s", exc)
            self._page = None
        if self._context:
            try:
                await self._context.close()
            except Exception as exc:
                logger.warning("Failed to close context: %s", exc)
            self._context = None
            self._context = None

    async def _ensure_logged_in(self) -> None:
        if not self._context:
            raise MavelyAutomationError("Persistent context has not been initialized")

        logger.debug("Opening persistent page for Mavely automation")
        self._page = await self._context.new_page()
        try:
            await self._page.goto(self.HOME_URL, wait_until="domcontentloaded", timeout=45000)
            current_url = self._page.url
            logger.debug("Loaded URL: %s", current_url)
            
            # If we landed on the login page, perform login
            if "login" in current_url.lower():
                logger.info("Mavely session requires login; attempting to authenticate")
                await self._perform_login(self._page)
                # After login, navigate back to home
                await self._page.goto(self.HOME_URL, wait_until="networkidle", timeout=45000)
            else:
                logger.debug("Mavely session already authenticated (not on login page)")
            
            # Final verification
            if "login" in self._page.url.lower():
                raise MavelyAutomationError("Failed to verify Mavely login state - still on login page")
            logger.info("Mavely authentication verified")
        except PlaywrightError as exc:
            # Clean up page on error
            if self._page:
                await self._page.close()
                self._page = None
            raise MavelyAutomationError(f"Failed during Mavely login flow: {exc}") from exc

    async def create_mavely_link(self, product_url: str) -> str:
        await self.start()

        if not self._context or not self._page:
            raise MavelyAutomationError("Persistent context or page is unavailable")

        async with self._lock:
            logger.info("Starting Mavely link creation for %s", product_url)
            try:
                # Make sure we're on the home page
                current_url = self._page.url
                logger.debug("Current page URL before link creation: %s", current_url)
                
                # If not on home, navigate there
                if not current_url.startswith(self.HOME_URL):
                    await self._page.goto(self.HOME_URL, wait_until="networkidle", timeout=45000)
                    logger.debug("Navigated to home, URL is now: %s", self._page.url)
                
                # Check if we got redirected to login page
                if "login" in self._page.url.lower():
                    logger.warning("Got redirected to login page, attempting to authenticate")
                    await self._perform_login(self._page)
                    await self._page.goto(self.HOME_URL, wait_until="networkidle", timeout=45000)
                    logger.debug("After re-login, URL is: %s", self._page.url)
                    if "login" in self._page.url.lower():
                        raise MavelyAutomationError("Still on login page after authentication attempt")
                
                # Check for and dismiss any modals/overlays that might be blocking the page
                await self._dismiss_modals(self._page)
                
                logger.debug("Locating link input field")
                input_box = await self._locate_link_input(self._page)
                logger.debug("Link input located; filling product URL")
                await input_box.fill(product_url)
                await self._trigger_generation(self._page, input_box)
                logger.debug("Waiting for link creation modal to appear...")
                
                # Wait for the "Link created!" modal to appear - poll until it appears
                max_attempts = 10
                for attempt in range(max_attempts):
                    await self._page.wait_for_timeout(1000)  # Wait 1 second between checks
                    modal_selector = "[role='dialog']"
                    if await self._page.locator(modal_selector).count() > 0:
                        logger.debug("Modal appeared after %d seconds", attempt + 1)
                        break
                else:
                    logger.warning("Modal did not appear after %d seconds", max_attempts)
                
                # Extract the link from the modal
                link = await self._extract_generated_link_from_modal(self._page)
                
                if link:
                    logger.info("Successfully extracted link from modal: %s", link)
                    # Click the "Copy Link" button in the modal
                    copy_button = await self._find_copy_button_in_modal(self._page)
                    if copy_button:
                        logger.debug("Clicking 'Copy Link' button")
                        await copy_button.click()
                        await self._page.wait_for_timeout(500)
                else:
                    logger.warning("Could not extract link from modal, trying alternative method")
                    link = await self._extract_generated_link(self._page)
            except PlaywrightError as exc:
                raise MavelyAutomationError(f"Failed to create Mavely link: {exc}") from exc

        if not link:
            raise MavelyAutomationError("Mavely did not return a new affiliate link")

        logger.info("Generated Mavely link for %s", product_url)
        return link

    async def _is_logged_in(self, page) -> bool:
        try:
            locator = page.locator("input[placeholder*='Enter URL']")
            count = await locator.count()
            logger.debug("Login check located %s matching inputs", count)
            return count > 0
        except PlaywrightError as exc:
            logger.debug("Login check failed: %s", exc)
            return False

    async def _perform_login(self, page) -> None:
        logger.debug("Performing login on current page (assuming we're on login page)")
        
        # We're already on the login page, just fill the form
        email_selectors = ["input[name='email']", "input[type='email']", "input#email", "input[autocomplete='username']"]
        password_selectors = ["input[name='password']", "input[type='password']", "input#password", "input[autocomplete='current-password']"]
        login_buttons = [
            "button:has-text('Sign in')",
            "button:has-text('sign in')",
            "button:has-text('Log in')",
            "button:has-text('Log In')",
            "button[type=submit]",
        ]

        # Fill email
        email_filled = False
        for selector in email_selectors:
            locator = page.locator(selector)
            count = await locator.count()
            if count > 0:
                logger.debug("Filling email field with selector %s (found %d)", selector, count)
                await locator.first.fill(self._email)
                email_filled = True
                break
        
        if not email_filled:
            raise MavelyAutomationError("Could not locate the Mavely email field")

        # Fill password
        password_filled = False
        for selector in password_selectors:
            locator = page.locator(selector)
            count = await locator.count()
            if count > 0:
                logger.debug("Filling password field with selector %s (found %d)", selector, count)
                await locator.first.fill(self._password)
                password_filled = True
                break
        
        if not password_filled:
            raise MavelyAutomationError("Could not locate the Mavely password field")

        # Click login button
        button_clicked = False
        for selector in login_buttons:
            locator = page.locator(selector)
            count = await locator.count()
            if count > 0:
                logger.info("Clicking login button with selector: %s (found %d elements)", selector, count)
                await locator.first.click()
                button_clicked = True
                break
        
        if not button_clicked:
            raise MavelyAutomationError("Unable to find the Mavely login button")
        
        # Wait for navigation after clicking login - give it time to authenticate and redirect
        logger.info("Waiting for authentication and redirect...")
        try:
            # Wait for URL to change away from login page (with longer timeout)
            await page.wait_for_url(lambda url: "login" not in url.lower(), timeout=30000)
            logger.info("Login successful! Redirected to: %s", page.url)
        except PlaywrightError as exc:
            logger.error("Login redirect timed out: %s", exc)
            # Give it a bit more time and check
            await page.wait_for_timeout(3000)
            logger.error("Current URL after timeout: %s", page.url)
            raise MavelyAutomationError("Login did not redirect from login page") from exc

    async def _extract_generated_link_from_modal(self, page) -> Optional[str]:
        """Extract the generated Mavely link from the 'Link created!' modal."""
        logger.debug("Looking for generated link in modal")
        
        # Look for text that contains "mavely.app.link" or "joinmavely.com"
        try:
            # Wait for modal to contain the link
            modal_selector = "[role='dialog']"
            if await page.locator(modal_selector).count() > 0:
                modal = page.locator(modal_selector).first
                
                # Try to find a link element in the modal first
                link_locator = modal.locator("a[href*='mavely'], a[href*='joinmavely']")
                if await link_locator.count() > 0:
                    href = await link_locator.first.get_attribute("href")
                    if href:
                        logger.info("Found link href in modal: %s", href)
                        return href
                
                text_content = await modal.text_content()
                
                # Look for mavely link patterns
                import re
                if text_content:
                    logger.info("Full modal text content (first 400 chars): %s", repr(text_content[:400]))
                    
                    # Match mavely.app.link URLs and capture exactly 11 characters for the link ID
                    match = re.search(r'https://mavely\.app\.link/e/([a-zA-Z0-9]{11})', text_content)
                    if match:
                        link_id = match.group(1)
                        link = f"https://mavely.app.link/e/{link_id}"
                        logger.info("✅ Extracted Mavely link from modal: %s", link)
                        return link
                    
                    # Fallback to joinmavely.com pattern
                    match = re.search(r'(https://[^\s]*joinmavely\.com/[A-Za-z0-9]+)', text_content)
                    if match:
                        raw_link = match.group(1)
                        link = ''.join(ch for ch in raw_link if ch.isalnum() or ch in {':', '/', '.', '-', '_'} )
                        logger.info("✅ Extracted Mavely link from modal: %s", link)
                        return link
                        
                    logger.warning("No mavely link pattern found in modal text")
                    logger.debug("Modal text for debugging: %s", text_content)
        except PlaywrightError as exc:
            logger.warning("Error extracting link from modal: %s", exc)
        
        return None
    
    async def _find_copy_button_in_modal(self, page):
        """Find the 'Copy Link' button in the modal."""
        logger.debug("Looking for 'Copy Link' button in modal")
        
        copy_selectors = [
            "[role='dialog'] button:has-text('Copy Link')",
            "[role='dialog'] button:has-text('Copy link')",
            "[role='dialog'] button:has-text('Copy')",
            "button:has-text('Copy Link')",
            "button:has-text('Copy link')",
        ]
        
        for selector in copy_selectors:
            try:
                count = await page.locator(selector).count()
                if count > 0:
                    logger.info("Found 'Copy Link' button with selector: %s", selector)
                    return page.locator(selector).first
            except PlaywrightError as exc:
                logger.debug("Selector %s failed: %s", selector, exc)
        
        logger.warning("Could not find 'Copy Link' button in modal")
        return None

    async def _dismiss_modals(self, page) -> None:
        """Check for and dismiss any modal dialogs or overlays that might block interaction."""
        logger.debug("Checking for modals or overlays that might block interaction")
        
        # Common close button selectors
        close_selectors = [
            "button[aria-label='Close']",
            "button[aria-label='close']",
            "[data-headlessui-state='open'] button",  # Headless UI close button
            ".modal button:has-text('Close')",
            ".modal button:has-text('×')",
            "[role='dialog'] button[aria-label*='close' i]",
            "[role='dialog'] button[aria-label*='dismiss' i]",
        ]
        
        for selector in close_selectors:
            try:
                count = await page.locator(selector).count()
                if count > 0:
                    logger.info("Found modal close button with selector: %s, clicking it", selector)
                    await page.locator(selector).first.click()
                    await page.wait_for_timeout(500)  # Give the modal time to close
                    return
            except PlaywrightError as exc:
                logger.debug("Failed to click close button with selector %s: %s", selector, exc)
        
        logger.debug("No modals found that need to be dismissed")

    async def _locate_link_input(self, page):
        # Try the Playwright locator API first (no wait, just check if present)
        placeholder_locator = page.get_by_placeholder("Enter URL to create a link")
        try:
            count = await placeholder_locator.count()
            logger.debug("Placeholder locator found %d matching inputs", count)
            if count > 0:
                logger.debug("Located link input via placeholder API")
                return placeholder_locator.first
        except PlaywrightError as exc:
            logger.debug("Placeholder API lookup failed: %s", exc)

        selectors = [
            "header input[placeholder='Enter URL to create a link']",
            "header input[placeholder*='create a link']",
            "input[placeholder*='Enter URL']",
            "input[aria-label*='Enter URL']",
            "input[name='url']",
            "input[type='url']",
            "input[data-testid*='link-input']",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = await locator.count()
                logger.debug("Checking selector %s for link input (found %d)", selector, count)
                if count > 0:
                    logger.debug("Selector %s matched; returning first element", selector)
                    return locator.first
            except PlaywrightError as exc:
                logger.debug("Selector %s raised error: %s", selector, exc)
                continue

        logger.debug("Falling back to any input[type=text] or input without type in the header")
        try:
            generic_inputs = page.locator("header input")
            count = await generic_inputs.count()
            logger.debug("Found %d generic input elements in header", count)
            if count > 0:
                logger.debug("Returning first header input as fallback")
                return generic_inputs.first
        except PlaywrightError as exc:
            logger.debug("Generic header input fallback failed: %s", exc)

        logger.error("All selectors exhausted; dumping page title and HTML for debugging")
        try:
            title = await page.title()
            logger.error("Page title: %s", title)
            
            # Take a screenshot
            screenshot_path = pathlib.Path("debug-mavely-screenshot.png")
            await page.screenshot(path=str(screenshot_path))
            logger.error("Screenshot saved to %s", screenshot_path)
            
            # Dump the page HTML to see what's actually rendered
            html = await page.content()
            html_path = pathlib.Path("debug-mavely-page.html")
            html_path.write_text(html, encoding="utf-8")
            logger.error("Page HTML saved to %s", html_path)
            
            # Try to find ALL inputs on the page
            all_inputs = page.locator("input")
            input_count = await all_inputs.count()
            logger.error("Found %d total input elements on the page", input_count)
            
            # Log details about each input
            for i in range(min(input_count, 10)):  # Limit to first 10
                input_elem = all_inputs.nth(i)
                placeholder = await input_elem.get_attribute("placeholder") or ""
                input_type = await input_elem.get_attribute("type") or ""
                name = await input_elem.get_attribute("name") or ""
                logger.error("Input %d: type=%s, name=%s, placeholder=%s", i, input_type, name, placeholder)
                
        except Exception as exc:
            logger.error("Failed to dump debug info: %s", exc)

        raise MavelyAutomationError("Could not find the Mavely link creation field")

    async def _trigger_generation(self, page, input_box) -> None:
        logger.debug("Triggering link generation via input interactions")
        await input_box.click()
        await page.wait_for_timeout(200)
        try:
            await input_box.press("Enter")
            logger.debug("Pressed Enter on link input")
        except PlaywrightError as exc:
            logger.debug("Enter key press failed: %s", exc)

        # Attempt to click any visible generate button/icon adjacent to the input.
        button_selectors = [
            "button[aria-label*='Create']",
            "button[aria-label*='Generate']",
            "button:has-text('Create link')",
            "button:has-text('Create Link')",
            "button:has-text('Generate link')",
            "button:has-text('Generate Link')",
            "[data-testid*='create-link']",
        ]
        for selector in button_selectors:
            locator = page.locator(selector)
            if await locator.count() > 0:
                await locator.first.click()
                logger.debug("Clicked button selector %s", selector)
                return

        # Fall back to clicking a sibling element if available.
        try:
            clicked = await input_box.evaluate(
                "(el) => {\n"
                "  const form = el.closest('form');\n"
                "  if (form) {\n"
                "    const button = form.querySelector('button, [role=button]');\n"
                "    if (button instanceof HTMLElement) {\n"
                "      button.click();\n"
                "      return true;\n"
                "    }\n"
                "  }\n"
                "  const sibling = el.nextElementSibling;\n"
                "  if (sibling instanceof HTMLElement) {\n"
                "    const childBtn = sibling.querySelector('button, [role=button]');\n"
                "    if (childBtn instanceof HTMLElement) {\n"
                "      childBtn.click();\n"
                "      return true;\n"
                "    }\n"
                "    sibling.click();\n"
                "    return true;\n"
                "  }\n"
                "  return false;\n"
                "}"
            )
            if clicked:
                logger.debug("Triggered generation using DOM fallback")
                return
        except PlaywrightError as exc:
            logger.debug("DOM fallback click failed: %s", exc)

    async def _wait_for_copy_button(self, page):
        selectors = [
            "button:has-text('Copy link')",
            "button:has-text('Copy Link')",
            "button:has-text('Copy URL')",
            "button[aria-label*='Copy']",
        ]
        for selector in selectors:
            try:
                button = await page.wait_for_selector(selector, timeout=45000)
                logger.debug("Copy button found via selector %s", selector)
                return button
            except PlaywrightError:
                continue
        return None

    async def _extract_generated_link(self, page) -> Optional[str]:
        # Scan the page (and active dialogs) for a URL pointing to mave.ly/mavely domains.
        selector_attr_pairs = [
            ("input[value*='mave.ly']", "value"),
            ("input[value*='mavely']", "value"),
            ("textarea[value*='mave.ly']", "value"),
            ("a[href*='mave.ly']", "href"),
            ("a[href*='mavely']", "href"),
        ]
        for selector, attr in selector_attr_pairs:
            try:
                element = await page.wait_for_selector(selector, timeout=15000)
                value = await element.get_attribute(attr)
                if value:
                    logger.debug("Found affiliate link in %s attribute via selector %s", attr, selector)
                    return value.strip()
            except PlaywrightError:
                continue

        # Fall back to scanning visible text nodes for the affiliate URL.
        try:
            handles = await page.query_selector_all(
                "xpath=//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'mave.ly')]"
            )
        except PlaywrightError:
            handles = []

        pattern = re.compile(r"https?://\S+", re.IGNORECASE)
        for handle in handles:
            try:
                text = (await handle.inner_text()).strip()
            except PlaywrightError:
                continue
            match = pattern.search(text)
            if match:
                url = match.group(0)
                if "mave.ly" in url or "mavely" in url:
                    logger.debug("Extracted affiliate link from text node: %s", url)
                    return url

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
            # Apply enhanced stealth tweaks
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            await context.add_init_script("window.chrome = {runtime: {}};")
            await context.add_init_script("Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});")
            await context.add_init_script("Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});")
            # Add more stealth: randomize viewport and mouse movements
            await context.add_init_script("""
                Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
                Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
                Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
            """)
            await context.add_init_script("Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});")
            await context.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Dest": "document",
                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
            })
            page = await context.new_page()
            # Randomize viewport
            viewports = [(1920, 1080), (1366, 768), (1536, 864)]
            width, height = random.choice(viewports)
            await page.set_viewport_size({"width": width, "height": height})
            close_after = True
        try:
            logger.debug("Navigating to URL...")
            # Change wait_until to "domcontentloaded" to avoid timeouts on sites with ongoing network activity (e.g., ads)
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            logger.debug("Waiting for page to fully load...")
            await asyncio.sleep(3.0)  # Wait longer for dynamic content
            # Add random mouse movements to simulate human behavior
            await page.mouse.move(random.randint(100, 500), random.randint(100, 500))
            await asyncio.sleep(1.0)
            await page.mouse.move(random.randint(100, 500), random.randint(100, 500))
            
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
            
            metadata = {
                "page_title": page_title,
                "og_title": await self._read_meta(page, "property", "og:title"),
                "og_price": await self._read_meta(page, "property", "product:price:amount"),
                "og_currency": await self._read_meta(page, "property", "product:price:currency"),
                "blocked": is_blocked,
            }
            return PageSnapshot(url=url, html=html, text=text, json_ld=json_ld, price_strings=price_strings, metadata=metadata)
        except PlaywrightError as exc:
            logger.exception("Playwright failed to load %s: %s", url, exc)
            return PageSnapshot(url=url, html="", text="", json_ld=[], price_strings=[], metadata={"error": str(exc)})
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
