import { generateAffiliateLink } from '../src/services/affiliate';

describe('generateAffiliateLink', () => {
    it('should return a valid affiliate link for a given URL', async () => {
        const url = 'https://example.com/product';
        const expectedAffiliateLink = 'https://mavely.com/affiliate?ref=12345'; // Replace with actual expected link

        const affiliateLink = await generateAffiliateLink(url);
        expect(affiliateLink).toBe(expectedAffiliateLink);
    });

    it('should throw an error for an invalid URL', async () => {
        const invalidUrl = 'invalid-url';

        await expect(generateAffiliateLink(invalidUrl)).rejects.toThrow('Invalid URL');
    });

    it('should handle empty URL input', async () => {
        const emptyUrl = '';

        await expect(generateAffiliateLink(emptyUrl)).rejects.toThrow('URL cannot be empty');
    });

    // Add more test cases as needed
});