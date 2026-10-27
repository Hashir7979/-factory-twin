import { defineConfig, devices } from "@playwright/test";

// E2E runs against the production build served by FastAPI (same origin), i.e.
// exactly what the Docker image ships. Build first: `npm run build`.
const port = Number(process.env.E2E_PORT ?? 8765);

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // Software WebGL so the 3D scene renders on CI machines without a GPU.
    launchOptions: { args: ["--use-gl=swiftshader", "--enable-unsafe-swiftshader"] },
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "mobile", use: { ...devices["Pixel 7"] }, testMatch: /mobile\.spec\.ts/ },
  ],
  webServer: {
    command: `python -m uvicorn api.main:app --host 127.0.0.1 --port ${port}`,
    cwd: "../backend",
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    env: { STATIC_DIR: "../frontend/dist", CORS_ORIGINS: "", SIM_WORKERS: "2" },
  },
});
