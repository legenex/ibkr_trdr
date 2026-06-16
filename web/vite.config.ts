import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, proxy /api to the FastAPI server so the browser hits one origin.
//
// Codespaces note: the dev server is reached over the forwarded
// https://<name>-5173.app.github.dev URL. Vite 7 blocks unknown Host headers
// and its HMR websocket defaults to ws on the local port, both of which fail
// behind the proxy. So we bind to all interfaces, allow the github.dev host,
// and tell HMR to use wss on 443.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    allowedHosts: [".app.github.dev"],
    hmr: {
      clientPort: 443,
      protocol: "wss",
    },
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "http://localhost:8000", ws: true },
    },
  },
});
