import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

/**
 * E2E runs against the real FastAPI backend pointed at the captured KTN-STL3
 * sysfs fixture, so the whole stack is exercised with no hardware attached.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.KTN_BASE_URL ?? "http://127.0.0.1:8421",
    trace: "off",
    screenshot: "off",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "mobile", use: { ...devices["Pixel 5"] } },
  ],
  webServer: {
    command: "bash scripts/e2e-server.sh",
    cwd: fileURLToPath(new URL("..", import.meta.url)),
    url: "http://127.0.0.1:8421/healthz",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
