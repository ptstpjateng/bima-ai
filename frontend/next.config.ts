import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    // BACKEND_URL is a server-side-only env var (no NEXT_PUBLIC_ prefix).
    // In production (Vercel): set BACKEND_URL=http://116.254.113.81
    // In local dev: falls back to NEXT_PUBLIC_API_URL so existing dev setup is unchanged.
    const backendUrl =
      process.env.BACKEND_URL ||
      process.env.NEXT_PUBLIC_API_URL ||
      "http://localhost:8080";
    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
