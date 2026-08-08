import { defineConfig } from '@playwright/test';


export default defineConfig({
  testDir: './tests/e2e',
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  outputDir: 'test-results/playwright',
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:8015',
    browserName: 'chromium',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: '.venv/bin/python tests/e2e/run_server.py',
    url: 'http://127.0.0.1:8015/accounts/login/',
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
