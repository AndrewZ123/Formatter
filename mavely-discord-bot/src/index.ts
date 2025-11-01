import { Client, GatewayIntentBits } from 'discord.js';
import { handleLink } from './commands/linkHandler';
import config from './config';

const client = new Client({
    intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages, GatewayIntentBits.MessageContent],
});

client.once('ready', () => {
    console.log(`Logged in as ${client.user?.tag}`);
});

client.on('messageCreate', async (message) => {
    if (!message.author.bot) {
        await handleLink(message);
    }
});

client.login(config.DISCORD_TOKEN);