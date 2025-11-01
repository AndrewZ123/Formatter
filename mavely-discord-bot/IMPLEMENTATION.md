# Discord Bot Login Implementation

## Overview
This document describes the implementation of the Mavely Discord bot login logic with environment variables.

## Architecture

### Environment Variables
The bot uses the following environment variables stored in the `.env` file:

- `DISCORD_BOT_TOKEN`: Discord bot authentication token
- `MAVELY_EMAIL`: Email address for Mavely account login
- `MAVELY_PASSWORD`: Password for Mavely account login
- `MAVELY_API_URL`: Base URL for Mavely API (optional)

### Authentication Flow

1. **Configuration Loading** (`src/config/index.ts`)
   - Loads environment variables using `dotenv`
   - Exports configuration object with all required credentials

2. **Authentication Service** (`src/services/auth.ts`)
   - `loginToMavely()`: Authenticates with Mavely API using email/password
   - `getAuthToken()`: Returns cached token or performs fresh login
   - Token caching with 1-hour expiry to minimize API calls
   - Automatic re-authentication when token expires

3. **Affiliate Link Generation** (`src/services/affiliate.ts`)
   - Calls `getAuthToken()` before making API requests
   - Uses authenticated token in Authorization header
   - Generates affiliate links from user-provided URLs

4. **Bot Logic** (`src/index.ts` and `src/bot.ts`)
   - Listens for messages in Discord
   - Extracts URLs from messages
   - Converts URLs to Mavely affiliate links
   - Responds with the generated affiliate link

## Security Considerations

1. **Credential Storage**
   - Credentials stored in `.env` file (not committed to git)
   - `.gitignore` ensures `.env` is never committed
   - `.env.example` provides template without actual credentials

2. **Token Management**
   - Authentication tokens cached in memory only
   - Automatic token refresh when expired
   - No tokens stored in files or persistent storage

## Usage

1. Set up `.env` file with your credentials:
   ```
   DISCORD_BOT_TOKEN=your_bot_token
   MAVELY_EMAIL=your_email@example.com
   MAVELY_PASSWORD=your_password
   ```

2. Start the bot:
   ```bash
   npm install
   npm start
   ```

3. Send any link in Discord channel where bot is present
4. Bot will authenticate with Mavely (if needed) and return affiliate link

## Testing

The implementation includes unit tests for:
- Authentication service (`tests/auth.test.ts`)
- Affiliate link generation (`tests/affiliate.test.ts`)

Run tests with:
```bash
npm test
```
