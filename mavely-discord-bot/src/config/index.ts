import dotenv from 'dotenv';

dotenv.config();

const config = {
    DISCORD_TOKEN: process.env.DISCORD_BOT_TOKEN || '',
    MAVELY_EMAIL: process.env.MAVELY_EMAIL || '',
    MAVELY_PASSWORD: process.env.MAVELY_PASSWORD || '',
    MAVELY_API_URL: process.env.MAVELY_API_URL || 'https://api.mavely.com/affiliate',
};

export default config;
export { config };