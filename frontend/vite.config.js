import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/candidate": "http://127.0.0.1:8765",
      "/reports": "http://127.0.0.1:8765",
      "/watchlist": "http://127.0.0.1:8765"
    }
  }
});
