from __future__ import annotations
from typing import Any, Dict, Optional
import base64
import json
import logging
import pathlib
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError

logger = logging.getLogger(__name__)


class LLMClient:
    """Thin wrapper around the OpenAI Chat Completions API with simple rate limiting."""

    def __init__(self, api_key: Optional[str], model: Optional[str]) -> None:
        self._api_key = api_key
        self._model = model or "gpt-4o"  # Default to GPT-4o for vision support
        self._client: Optional[AsyncOpenAI] = None

    @property
    def enabled(self) -> bool:
        return self._api_key is not None

    async def start(self) -> None:
        if not self.enabled:
            return
        self._client = AsyncOpenAI(api_key=self._api_key)

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def extract_product_fields(self, page_text: str, metadata: Dict[str, Any], screenshot_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            logger.debug("LLM is not enabled, skipping")
            return None

        prompt = self._build_prompt(page_text, metadata)
        logger.debug("Sending to LLM: prompt length %d, screenshot: %s", len(prompt), screenshot_path is not None)
        content = await self._call_llm(prompt, screenshot_path)
        if not content:
            logger.warning("LLM returned empty content")
            return None

        try:
            result = json.loads(content)
            return result
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse LLM response as JSON: %s", exc)
            logger.debug("Raw LLM response: %s", content)
            return None

    async def _call_llm(self, content: str, screenshot_path: Optional[str] = None) -> str:
        if not self._client:
            raise RuntimeError("LLM client not initialized")

        messages = [
            {
                "role": "system",
                "content": "You are an expert at extracting product information from e-commerce pages. Carefully scan all provided text and images for product titles and prices. Look for patterns like '$99.99', 'Was $200', 'Now $150', '80% off', currency symbols, and numbers near price keywords. For blocked pages, the full content is provided - search thoroughly. Return only valid JSON with product_title (string), original_price (string number or null), sale_price (string number or null).",
            }
        ]

        user_content = []
        if screenshot_path and pathlib.Path(screenshot_path).exists():
            with open(screenshot_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode('utf-8')
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{base64_image}"}
            })
        
        user_content.append({
            "type": "text",
            "text": content
        })
        
        messages.append({
            "role": "user",
            "content": user_content
        })

        @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
        async def _attempt():
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=300,  # Increased for more complete responses
                    temperature=0.1,
                )
                return response.choices[0].message.content
            except (APIError, APITimeoutError, RateLimitError) as exc:
                logger.warning("OpenAI API error: %s", exc)
                raise

        try:
            return await _attempt()
        except RetryError as exc:
            logger.error("Failed to call LLM after retries: %s", exc)
            return ""

    def _build_prompt(self, page_text: str, metadata: Dict[str, Any]) -> str:
        trimmed_text = page_text.strip()
        # Aggressively trim to reduce tokens - focus on first 4000 chars which usually has product info
        if len(trimmed_text) > 4000:
            trimmed_text = trimmed_text[:4000]
        
        # Get URL from metadata to help with extraction
        url = metadata.get("url", "")
        blocked = metadata.get("blocked", False)
        
        # Create a minimal prompt focusing only on essential extraction
        prompt = f"""Extract product information from this e-commerce page.

URL: {url}
Page metadata: {json.dumps(metadata)}
Page content: {trimmed_text}

INSTRUCTIONS:
1. Extract a clean, simplified product title without color, size, pack quantity, or other variations. Focus on the main product name only (e.g., "DURASACK HEAVY DUTY BAG" instead of "DURASACK HEAVY DUTY BAG, PACK OF 8, ORANGE")
2. Look for prices in metadata (og_price) or page text. If there are multiple prices, assume the lowest is the sale_price and the highest is the original_price.
3. If the page is blocked ("Access Denied"), extract what you can from the URL structure
4. For URLs like "macys.com/shop/product/diamond-circle-leverback-drop-earrings-1-4-ct.-tw-in-sterling-silver-created-for-macys", extract a clean product name
5. If blocked, infer prices from URL patterns (e.g., 'price-79.99', '$X.XX', query params with 'price', or discount hints like '80% off')

Return JSON with:
- product_title: The simplified main product name/title (cleaned and formatted)
- original_price: Regular price (numbers only, e.g. "99.99")
- sale_price: Current/sale price (numbers only, e.g. "79.99")

If you cannot find prices, set them to null. Focus on extracting the best simplified product title possible."""
        
        return prompt
