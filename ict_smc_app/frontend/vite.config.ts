import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on :5173 proxies the API and WebSocket to the FastAPI backend on :8600.
// `npm run build` writes dist/, which the backend serves itself at http://<laptop>:8600/.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8600",
      "/ws": { target: "ws://127.0.0.1:8600", ws: true },
    },
  },
});
