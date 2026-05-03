import type { NextConfig } from "next";

// Comma-separated list of origins that may load the dev server. Defaults
// to localhost; if you want to open the dashboard from another device on
// your LAN (phone / tablet), set NEXT_DEV_ORIGINS in .env.local — e.g.
//   NEXT_DEV_ORIGINS=192.168.1.10
const devOrigins = (process.env.NEXT_DEV_ORIGINS ?? "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

const nextConfig: NextConfig = {
  allowedDevOrigins: devOrigins,
  // Hide the on-screen route indicator that floats in the bottom-left
  // during dev — build/runtime errors still surface.
  devIndicators: false,
};

export default nextConfig;
