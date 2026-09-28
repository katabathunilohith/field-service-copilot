import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The FastAPI backend (python main.py) serves /api; the dev server proxies to it.
const apiTarget = process.env.VITE_API_URL || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/api": { target: apiTarget } },
  },
});
