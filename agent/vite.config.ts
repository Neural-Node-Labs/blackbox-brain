import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During local dev, proxy /api to the gateway container so the browser
// never needs CORS at all on localhost. In production the same /api
// path is proxied by nginx (see nginx.conf), keeping the client code
// identical across environments.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_GATEWAY_ORIGIN || "http://localhost:8080",
        changeOrigin: true,
      },
    },
  },
});
