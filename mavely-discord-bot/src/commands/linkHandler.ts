import { Message } from 'discord.js';
import { generateAffiliateLink } from '../services/affiliate';

export const handleLink = async (message: Message) => {
    const urlRegex = /(https?:\/\/[^\s]+)/g;
    const urls = message.content.match(urlRegex);

    if (urls && urls.length > 0) {
        const originalLink = urls[0];
        try {
            const affiliateLink = await generateAffiliateLink(originalLink);
            message.reply(`Here is your Mavely affiliate link: ${affiliateLink}`);
        } catch (error) {
            console.error('Error generating affiliate link:', error);
            message.reply('There was an error generating your affiliate link. Please try again later.');
        }
    } else {
        message.reply('Please send a valid link to generate an affiliate link.');
    }
};