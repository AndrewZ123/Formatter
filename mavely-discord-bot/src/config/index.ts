import dotenv from 'dotenv';

dotenv.config();

const config = {
    DISCORD_TOKEN: process.env.DISCORD_TOKEN || '',
    MAVELY_API_KEY: process.env.MAVELY_API_KEY || '',
    MAVELY_API_URL: process.env.MAVELY_API_URL || 'https://api.mavely.com/affiliate',
};

export default config;