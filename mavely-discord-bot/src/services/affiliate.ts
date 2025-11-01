import axios from 'axios';
import { AffiliateResponse } from '../types';

const MAVELY_API_URL = 'https://api.mavely.com/affiliate/link'; // Replace with the actual Mavely API endpoint
const MAVELY_API_KEY = process.env.MAVELY_API_KEY; // Ensure this is set in your environment variables

export const generateAffiliateLink = async (url: string): Promise<string | null> => {
    try {
        const response = await axios.post<AffiliateResponse>(MAVELY_API_URL, {
            url: url,
        }, {
            headers: {
                'Authorization': `Bearer ${MAVELY_API_KEY}`,
                'Content-Type': 'application/json',
            },
        });

        if (response.data && response.data.affiliateLink) {
            return response.data.affiliateLink;
        } else {
            console.error('No affiliate link returned from Mavely API');
            return null;
        }
    } catch (error) {
        console.error('Error generating affiliate link:', error);
        return null;
    }
};