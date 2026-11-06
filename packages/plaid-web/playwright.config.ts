import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: 'tests',
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:8797',
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined },
  },
  webServer: {
    command: 'uv run --project ../.. python ../../tests/web_fixture.py',
    url: 'http://127.0.0.1:8797',
    reuseExistingServer: false,
  },
});
