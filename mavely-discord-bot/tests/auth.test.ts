import { loginToMavely, getAuthToken, clearAuthToken } from '../src/services/auth';
import axios from 'axios';

// Mock axios
jest.mock('axios');
const mockedAxios = axios as jest.Mocked<typeof axios>;

describe('Mavely Authentication Service', () => {
    beforeEach(() => {
        // Clear cached tokens before each test
        clearAuthToken();
        jest.clearAllMocks();
    });

    describe('loginToMavely', () => {
        it('should successfully login and return auth token', async () => {
            const mockToken = 'mock-auth-token-12345';
            mockedAxios.post.mockResolvedValue({
                data: {
                    token: mockToken,
                },
            });

            const token = await loginToMavely();

            expect(token).toBe(mockToken);
            expect(mockedAxios.post).toHaveBeenCalledWith(
                'https://api.mavely.com/auth/login',
                expect.objectContaining({
                    email: expect.any(String),
                    password: expect.any(String),
                }),
                expect.objectContaining({
                    headers: {
                        'Content-Type': 'application/json',
                    },
                })
            );
        });

        it('should return null when login fails', async () => {
            mockedAxios.post.mockRejectedValue(new Error('Login failed'));

            const token = await loginToMavely();

            expect(token).toBeNull();
        });

        it('should cache token and not login again if token is valid', async () => {
            const mockToken = 'mock-auth-token-12345';
            mockedAxios.post.mockResolvedValue({
                data: {
                    token: mockToken,
                },
            });

            // First call should login
            const token1 = await loginToMavely();
            expect(token1).toBe(mockToken);
            expect(mockedAxios.post).toHaveBeenCalledTimes(1);

            // Second call should use cached token
            const token2 = await loginToMavely();
            expect(token2).toBe(mockToken);
            expect(mockedAxios.post).toHaveBeenCalledTimes(1); // Still only called once
        });
    });

    describe('getAuthToken', () => {
        it('should return auth token by calling loginToMavely', async () => {
            const mockToken = 'mock-auth-token-12345';
            mockedAxios.post.mockResolvedValue({
                data: {
                    token: mockToken,
                },
            });

            const token = await getAuthToken();

            expect(token).toBe(mockToken);
        });
    });

    describe('clearAuthToken', () => {
        it('should clear cached token forcing fresh login', async () => {
            const mockToken = 'mock-auth-token-12345';
            mockedAxios.post.mockResolvedValue({
                data: {
                    token: mockToken,
                },
            });

            // First login
            await loginToMavely();
            expect(mockedAxios.post).toHaveBeenCalledTimes(1);

            // Clear token
            clearAuthToken();

            // Second login should make another API call
            await loginToMavely();
            expect(mockedAxios.post).toHaveBeenCalledTimes(2);
        });
    });
});
