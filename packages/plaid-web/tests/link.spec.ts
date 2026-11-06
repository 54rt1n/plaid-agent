import { expect, test } from '@playwright/test';

test('operator login, OAuth return, connection renewal, cancellation and logout', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('https://cdn.plaid.com/**', async (route) => {
    await route.fulfill({
      contentType: 'application/javascript',
      body: `window.Plaid = { create(config) {
        return { destroy() {}, open() {
          if (sessionStorage.getItem('mock-cancel') || (config.receivedRedirectUri && sessionStorage.getItem('mock-cancel-on-return'))) {
            setTimeout(() => config.onExit(null, {}), 0);
          } else if (config.receivedRedirectUri || sessionStorage.getItem('mock-direct-success')) {
            setTimeout(() => config.onSuccess('test-public', { institution: { name: 'Example Bank' } }), 0);
          } else {
            window.location.assign('/oauth-return?oauth_state_id=browser-fixture');
          }
        }};
      }};`,
    });
  });

  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Start from your terminal' })).toBeVisible();
  await page.goto('/#setup=browser-test-setup');
  await expect(page.getByRole('button', { name: 'Connect an institution' })).toBeEnabled();
  expect(new URL(page.url()).hash).toBe('');
  const cookies = await page.context().cookies();
  expect(cookies.find((x) => x.name === 'plaid_session')?.httpOnly).toBe(true);

  await page.getByRole('button', { name: 'Connect an institution' }).click();
  await expect(page.getByRole('heading', { name: 'Example Bank' })).toBeVisible();
  await expect(page.getByRole('status')).toHaveText('Account access updated.');
  expect(new URL(page.url()).pathname).toBe('/');
  expect(new URL(page.url()).search).toBe('');

  await page.getByRole('button', { name: 'Renew access' }).click();
  await expect(page.getByRole('status')).toHaveText('Account access updated.');
  await expect(page.getByRole('heading', { name: 'Example Bank' })).toHaveCount(1);

  await page.evaluate(() => sessionStorage.setItem('mock-cancel', 'true'));
  await page.getByRole('button', { name: 'Connect an institution' }).click();
  await expect(page.getByRole('button', { name: 'Connect an institution' })).toBeEnabled();
  await expect(page.getByRole('status')).toHaveCount(0);
  await expect(page.getByRole('alert')).toHaveCount(0);

  await page.evaluate(() => sessionStorage.removeItem('mock-cancel'));
  await page.getByRole('button', { name: 'Connect an institution' }).click();
  await expect(page.getByRole('heading', { name: 'Example Bank' })).toHaveCount(2);
  await expect(page.getByRole('button', { name: 'Renew access for Example Bank (connection-one)', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Renew access for Example Bank (connection-2)', exact: true })).toBeVisible();
  await expect(page.getByText('connection-one', { exact: true })).toBeVisible();
  await expect(page.getByText('connection-2', { exact: true })).toBeVisible();

  // A cancelled return must not poison a later direct connection or its reload.
  await page.evaluate(() => sessionStorage.setItem('mock-cancel-on-return', 'true'));
  await page.getByRole('button', { name: 'Connect an institution' }).click();
  await expect(page.getByRole('button', { name: 'Connect an institution' })).toBeEnabled();
  expect(new URL(page.url()).pathname).toBe('/');
  await page.evaluate(() => {
    sessionStorage.removeItem('mock-cancel-on-return');
    sessionStorage.setItem('mock-direct-success', 'true');
  });
  await page.getByRole('button', { name: 'Connect an institution' }).click();
  await expect(page.getByRole('heading', { name: 'Example Bank' })).toHaveCount(3);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Example Bank' })).toHaveCount(3);
  await expect(page.getByRole('alert')).toHaveCount(0);

  // The server completes but its response is lost; retry the same completion.
  await page.route('**/api/link/complete', async (route) => {
    await route.fetch();
    await route.abort('failed');
  }, { times: 1 });
  await page.getByRole('button', { name: 'Connect an institution' }).click();
  await expect(page.getByRole('button', { name: 'Retry finishing connection' })).toBeEnabled();
  await page.getByRole('button', { name: 'Retry finishing connection' }).click();
  await expect(page.getByRole('heading', { name: 'Example Bank' })).toHaveCount(4);
  await expect(page.getByRole('alert')).toHaveCount(0);

  // Reload also recovers the stored completion without creating a new Link flow.
  await page.route('**/api/link/complete', async (route) => {
    await route.fetch();
    await route.abort('failed');
  }, { times: 1 });
  await page.getByRole('button', { name: 'Connect an institution' }).click();
  await expect(page.getByRole('button', { name: 'Retry finishing connection' })).toBeEnabled();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Example Bank' })).toHaveCount(5);
  await expect(page.getByRole('alert')).toHaveCount(0);

  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Start from your terminal' })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem('plaid-agent-link-session'))).toBeNull();
  expect(errors).toEqual([]);
});

test('setup permits multiple connections and renewal until Finish setup', async ({ page }) => {
  await page.route('https://cdn.plaid.com/**', (route) => route.fulfill({
    contentType: 'application/javascript',
    body: `window.Plaid = { create(config) { return { destroy() {}, open() {
      setTimeout(() => config.onSuccess('test-public', {institution: {name: 'Setup bank'}}), 0);
    } }; } };`,
  }));
  let stopped = false;
  let loseCompletion = true;
  let loseFinish = true;
  let callsAfterFinish = 0;
  await page.route('**/api/status', async (route) => {
    if (stopped) { callsAfterFinish++; await route.abort(); }
    else await route.continue();
  });
  await page.route('**/api/link/complete', async (route) => {
    const response = await route.fetch();
    if (loseCompletion) { loseCompletion = false; await route.abort(); }
    else await route.fulfill({response});
  });
  await page.route('**/api/setup/finish', async (route) => {
    if (loseFinish) {
      loseFinish = false;
      await route.abort();
      return;
    }
    const response = await route.fetch();
    stopped = true;
    await route.fulfill({response});
  });
  await page.goto('/#setup=browser-test-setup');
  await page.getByRole('button', {name: 'Connect an institution'}).click();
  await expect(page.getByRole('button', {name: 'Retry finishing connection'})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Finish setup'})).toBeDisabled();
  await page.getByRole('button', {name: 'Retry finishing connection'}).click();
  await expect(page.getByRole('heading', {name: 'Setup bank', exact: true})).toHaveCount(1);
  await expect(page.getByRole('button', {name: 'Connect an institution'})).toBeEnabled();
  await page.getByRole('button', {name: 'Connect an institution'}).click();
  await expect(page.getByRole('heading', {name: 'Setup bank', exact: true})).toHaveCount(2);
  await page.getByRole('button', {name: /Renew access for Setup bank/}).first().click();
  await expect(page.getByRole('heading', {name: 'Setup bank', exact: true})).toHaveCount(2);
  await expect(page.getByRole('button', {name: 'Finish setup'})).toBeEnabled();
  await expect(page.getByRole('heading', {name: 'Setup finished'})).toHaveCount(0);

  await page.getByRole('button', {name: 'Finish setup'}).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByRole('button', {name: 'Finish setup'})).toBeEnabled();
  await page.getByRole('button', {name: 'Finish setup'}).click();
  await expect(page.getByRole('heading', {name: 'Setup finished'})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Connect an institution'})).toHaveCount(0);
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect(callsAfterFinish).toBe(0);
  // Reopening a new setup URL in the same tab must leave the completed screen.
  stopped = false;
  await page.goto('/#setup=browser-test-setup');
  await page.getByRole('button', {name: 'Connect an institution'}).click();
  await expect(page.getByRole('heading', {name: 'Setup bank', exact: true})).toHaveCount(3);
  await page.getByRole('button', {name: 'Finish setup'}).click();
  await expect(page.getByRole('heading', {name: 'Setup finished'})).toBeVisible();
  expect(callsAfterFinish).toBe(0);
});
