# Mavely Discord Bot

This project is a Discord bot that converts any link sent to it into a Mavely affiliate link and sends the generated link back to the user.

## Features

- Listens for messages in Discord channels.
- Detects links in messages.
- Converts detected links into Mavely affiliate links.
- Responds with the generated affiliate link.

## Prerequisites

- Node.js (version 14 or higher)
- TypeScript
- A Discord account and a Discord bot token.
- A Mavely account with email and password credentials.

## Setup

1. Clone the repository:

   ```bash
   git clone <repository-url>
   cd mavely-discord-bot
   ```

2. Install dependencies:

   ```bash
   npm install
   ```

3. Create a `.env` file in the root directory based on the `.env.example` file and fill in your Discord bot token and Mavely account credentials (email and password).

   Required environment variables:
   - `DISCORD_BOT_TOKEN`: Your Discord bot token
   - `MAVELY_EMAIL`: Your Mavely account email
   - `MAVELY_PASSWORD`: Your Mavely account password
   - `MAVELY_API_URL`: Mavely API base URL (optional, defaults to https://api.mavely.com/affiliate/generate)

4. Run the bot:

   ```bash
   npm start
   ```

## Usage

- Invite the bot to your Discord server.
- Send any link in a channel where the bot is present.
- The bot will respond with the corresponding Mavely affiliate link.

## Testing

To run the tests, use the following command:

```bash
npm test
```

## Contributing

Contributions are welcome! Please open an issue or submit a pull request for any improvements or bug fixes.

## License

This project is licensed under the MIT License. See the LICENSE file for details.