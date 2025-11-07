playrigh

Python-based Discord bot that turns product URLs into Mavely affiliate links and replies with enriched product metadata.

## Features
- Watches a target Discord channel for messages containing URLs
- Automates the Mavely dashboard via Playwright with a persistent profile
- Multi-stage product extraction pipeline (browser scrape → HTTP fallback → LLM)
- Optional site-specific scraping profiles via `site-profiles.json`
- Structured Discord replies with affiliate link, title, price before/after, currency, and confidence indicator

## Project Layout
```
src/
	bot.py            # Discord entry point
	mavely.py         # Playwright automation for Mavely link creation
	extractor.py      # Product extraction pipeline and fallbacks
	llm.py            # OpenAI helper with simple rate limiting
	utils.py          # Shared config, models, and Playwright session tooling
requirements.txt    # Python dependencies
.env.example        # Documented environment variables (no secrets)
site-profiles.json  # Site-specific scraping rules (optional)
.mavely-profile/    # Persistent Chromium profile (created at runtime, ignored by git)
```

## Setup
1. Create and activate your preferred Python 3.10+ virtual environment.
2. Install dependencies:
	 ```bash
	 pip install -r requirements.txt
	 playwright install
	 ```
3. Copy `.env.example` to `.env` and populate the variables (no secrets are committed).

## Required Environment Variables
| Name | Description |
| --- | --- |
| `DISCORD_TOKEN` | Bot token with *Message Content Intent* enabled |
| `CHANNEL_ID` | Numeric ID of the Discord channel to monitor |
| `MAVELY_EMAIL` / `MAVELY_PASSWORD` | Credentials used to keep the Mavely session logged in |
| `OPENAI_API_KEY` | Key used for LLM fallback (optional but recommended) |
| `OPENAI_MODEL` | Override to target a specific OpenAI model (defaults to `gpt-4o-mini`) |
| `HEADFUL` | Set to `true` to watch the Playwright browser window |
| `DEBUG_EXTRACT` | Set to `true` to emit JSON artifacts per extraction |
| `DEBUG_DIR` | Output directory for debug payloads (default `debug-artifacts`) |

## Running the Bot
```bash
python -m src.bot
```

The first run launches Chromium with a persistent profile stored under `.mavely-profile/`. Ensure the credentials supplied in the environment variables are valid so the login can succeed.

## Site Profiles
Populate `site-profiles.json` with domain-based selectors to override extraction logic. Example schema:
```json
{
	"example.com": {
		"title": ["h1.product-title"],
		"original_price": ["span.price-original"],
		"sale_price": ["span.price-sale"],
		"currency": "USD"
	}
}
```

## Debugging
- Enable `DEBUG_EXTRACT=true` to capture intermediate artifacts in the directory defined by `DEBUG_DIR`.
- Logs use standard output; increase verbosity with the same flag.
- Handle rate limits by adjusting the semaphore in `LLMClient` or tweaking Playwright timeouts.