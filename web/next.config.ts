import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Read-only viewer over local pipeline output; nothing is fetched from Google or Groq.
  poweredByHeader: false,
};

export default nextConfig;
