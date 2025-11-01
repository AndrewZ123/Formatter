import axios from 'axios';
import { AffiliateResponse } from '../types';
import { getAuthToken } from './auth';
import config from '../config';

export const generateAffiliateLink = async (url: string): Promise<string | null> => {
    try {
        // Get authenticated token
        const authToken = await getAuthToken();
        
        if (!authToken) {
            console.error('Failed to authenticate with Mavely');
            return null;
        }

        const response = await axios.post<AffiliateResponse>(
            `${config.MAVELY_API_URL}/link`,
            {
                url: url,
            },
            {
                headers: {
                    'Authorization': `Bearer ${authToken}`,
                    'Content-Type': 'application/json',
                },
            }
        );

        if (response.data && response.data.affiliateLink) {
            return response.data.affiliateLink;
        } else {
            console.error('No affiliate link returned from Mavely API');
            return null;
        }
    } catch (error) {
        if (axios.isAxiosError(error)) {
            console.error('Error generating affiliate link:', error.response?.data || error.message);
        } else {
            console.error('Error generating affiliate link:', error);
        }
        return null;
    }
};