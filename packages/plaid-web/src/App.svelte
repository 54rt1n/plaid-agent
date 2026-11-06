<script lang="ts">
  import { onMount } from 'svelte';
  import { ApiError, finishSetup, getStatus, signIn, signOut, type Status } from './lib/api';
  import { clearSession, finishPendingConnection, hasOAuthReturn, hasPendingCompletion, openLink } from './lib/link';

  let status = $state<Status | null>(null);
  let busy = $state(true);
  let message = $state('');
  let error = $state('');
  let signedOut = $state(false);
  let pendingCompletion = $state(false);
  let finished = $state(false);

  function explain(reason: unknown): void {
    if (reason instanceof ApiError && reason.status === 401) signedOut = true;
    error = reason instanceof Error ? reason.message : 'The request could not complete.';
  }

  async function connect(connectionId?: string, resume = false): Promise<void> {
    busy = true;
    error = '';
    message = '';
    try {
      const outcome = await openLink(connectionId, resume);
      if (outcome === 'connected') message = 'Account access updated.';
      status = await getStatus();
    } catch (reason) { explain(reason); }
    finally { pendingCompletion = hasPendingCompletion(); busy = false; }
  }

  async function retryCompletion(): Promise<void> {
    busy = true;
    error = '';
    message = '';
    try {
      await finishPendingConnection();
      message = 'Account access updated.';
      status = await getStatus();
    } catch (reason) { explain(reason); }
    finally { pendingCompletion = hasPendingCompletion(); busy = false; }
  }

  async function finish(): Promise<void> {
    busy = true;
    error = '';
    message = '';
    try {
      await finishSetup();
      finished = true;
    } catch (reason) { explain(reason); }
    finally { busy = false; }
  }

  async function logout(): Promise<void> {
    try {
      await signOut();
      clearSession();
      pendingCompletion = false;
      status = null;
      signedOut = true;
    } catch (reason) { explain(reason); }
  }

  onMount(() => {
    async function initialize(): Promise<void> {
      busy = true;
      error = '';
      message = '';
      finished = false;
      signedOut = false;
      try {
        const setup = new URLSearchParams(location.hash.slice(1)).get('setup');
        // Remove the operator credential before opening any third-party UI.
        if (setup) {
          history.replaceState(null, '', location.pathname + location.search);
          await signIn(setup);
        }
        status = await getStatus();
        if (hasPendingCompletion()) await retryCompletion();
        else if (hasOAuthReturn()) await connect(undefined, true);
      } catch (reason) { explain(reason); }
      finally { busy = false; }
    }
    function setupLinkChanged(): void {
      if (new URLSearchParams(location.hash.slice(1)).has('setup')) void initialize();
    }
    window.addEventListener('hashchange', setupLinkChanged);
    void initialize();
    return () => window.removeEventListener('hashchange', setupLinkChanged);
  });
</script>

<svelte:head><meta name="description" content="Connect and manage your financial account access." /></svelte:head>

<main>
  <header>
    <a class="brand" href="/">Plaid Agent</a>
    {#if status && !finished}<button class="quiet" onclick={logout}>Sign out</button>{/if}
  </header>

  <section class="intro">
    <p class="eyebrow">Account access</p>
    <h1>Your connected accounts.</h1>
    <p>Connect your financial institutions or renew access to an existing connection.</p>
  </section>

  {#if error}<p class="notice error" role="alert">{error}</p>{/if}
  {#if message}<p class="notice" role="status">{message}</p>{/if}

  {#if finished}
    <section class="card" role="status">
      <h2>Setup finished</h2>
      <p>Your connected accounts have been saved. You can close this window.</p>
    </section>
  {:else if signedOut}
    <section class="card">
      <h2>Start from your terminal</h2>
      <p>Run the same connect command you used to open this page to sign in again.</p>
    </section>
  {:else if status}
    <div class="toolbar">
      <span class:production={status.environment === 'production'} class="badge">{status.environment === 'sandbox' ? 'Sandbox · test accounts' : 'Live accounts'}</span>
      {#if pendingCompletion}
        <div>
          <button disabled={busy} onclick={retryCompletion}>Retry finishing connection</button>
          <button class="secondary" disabled={busy} onclick={() => connect()}>Start a new connection</button>
        </div>
      {:else}
        <button disabled={busy} onclick={() => connect()}>Connect an institution</button>
      {/if}
      <button class="secondary" disabled={busy || pendingCompletion} onclick={finish}>Finish setup</button>
    </div>
    {#if status.connections.length === 0}
      <section class="card empty"><h2>No institutions connected yet</h2><p>Select “Connect an institution” to choose your bank and the accounts you want to share.</p></section>
    {:else}
      <div class="connections">
        {#each status.connections as connection (connection.id)}
          <article class="card">
            <h2>{connection.institution ?? 'Connected institution'}</h2>
            <p class="muted">Connection <code>{connection.id}</code></p>
            <p>{connection.products.map((x) => x[0].toUpperCase() + x.slice(1)).join(' · ')}</p>
            <p class="muted">{connection.last_transaction_sync ? `Transactions last synced ${new Date(connection.last_transaction_sync).toLocaleString()}` : 'Transactions have not been synced yet.'}</p>
            <button class="secondary" aria-label={`Renew access for ${connection.institution ?? 'institution'} (${connection.id})`} disabled={busy || pendingCompletion} onclick={() => connect(connection.id)}>Renew access</button>
          </article>
        {/each}
      </div>
    {/if}
    <p class="footnote">Bank sign-in is handled by Plaid and your institution. Connecting an account does not authorize payments or transfers.</p>
  {:else if busy}
    <p role="status">Loading account connections…</p>
  {/if}
</main>
