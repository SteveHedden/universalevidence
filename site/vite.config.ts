import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/openapi.json": "http://localhost:8000",
      "/stats": "http://localhost:8000",
      "/query": "http://localhost:8000",
      "/taxonomy": "http://localhost:8000",
      "/graph": "http://localhost:8000"
    }
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts"
  }
});
