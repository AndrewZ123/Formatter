// This file defines TypeScript types and interfaces used throughout the project.

export interface LinkMessage {
    content: string;
    authorId: string;
    channelId: string;
}

export interface AffiliateResponse {
    success: boolean;
    affiliateLink?: string;
    error?: string;
}