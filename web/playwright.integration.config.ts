import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e-integration",
  fullyParallel: false,
  workers: 1,
  timeout: 90000,
  use: {
    baseURL: "http://127.0.0.1:5173",
    channel: process.platform === "win32" ? "msedge" : undefined,
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run build && npm run preview -- --port 5173 --strictPort",
    url: "http://127.0.0.1:5173",
    env: { VITE_API_BASE_URL: "http://127.0.0.1:5173" },
    reuseExistingServer: false,
  },
});
