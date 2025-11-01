import axios from 'axios';
import config from '../config';

const TOKEN_EXPIRY_HOURS = 1; // Token expires after 1 hour

let authToken: string | null = null;
let tokenExpiry: Date | null = null;

/**
 * Logs into the Mavely account using credentials from environment variables
 * @returns Authentication token if successful, null otherwise
 */
export const loginToMavely = async (): Promise<string | null> => {
    try {
        // Check if we already have a valid token
        if (authToken && tokenExpiry && new Date() < tokenExpiry) {
            return authToken;
        }

        const loginUrl = 'https://api.mavely.com/auth/login';
        const response = await axios.post(loginUrl, {
            email: config.MAVELY_EMAIL,
            password: config.MAVELY_PASSWORD,
        }, {
            headers: {
                'Content-Type': 'application/json',
            },
        });

        if (response.data && response.data.token) {
            authToken = response.data.token;
            // Set token expiry based on TOKEN_EXPIRY_HOURS constant
            tokenExpiry = new Date(Date.now() + TOKEN_EXPIRY_HOURS * 60 * 60 * 1000);
            console.log('Successfully logged into Mavely account');
            return authToken;
        } else {
            console.error('Login response did not contain a token');
            return null;
        }
    } catch (error) {
        if (axios.isAxiosError(error)) {
            console.error('Error logging into Mavely:', error.response?.data || error.message);
        } else {
            console.error('Error logging into Mavely:', error);
        }
        return null;
    }
};

/**
 * Gets the current authentication token, logging in if necessary
 * @returns Authentication token if successful, null otherwise
 */
export const getAuthToken = async (): Promise<string | null> => {
    return await loginToMavely();
};

/**
 * Clears the cached authentication token, forcing a fresh login on next request
 */
export const clearAuthToken = (): void => {
    authToken = null;
    tokenExpiry = null;
};
