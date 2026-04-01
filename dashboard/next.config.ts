import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  basePath: process.env.NEXT_PUBLIC_BASE_PATH || "",
  transpilePackages: [
    "@cloudscape-design/components",
    "@cloudscape-design/component-toolkit",
  ],
};

export default nextConfig;
