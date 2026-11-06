import { completeSession, createSession, type Finish, type LinkSession } from './api';

const storageKey = 'plaid-agent-link-session';
const completionKey = 'plaid-agent-link-completion';
let sdk: Promise<Window['Plaid']> | undefined;

function loadSdk(): Promise<Window['Plaid']> {
  if (window.Plaid) return Promise.resolve(window.Plaid);
  if (sdk) return sdk;
  sdk = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js';
    script.onload = () => {
      if (window.Plaid) resolve(window.Plaid);
      else { sdk = undefined; script.remove(); reject(new Error('Plaid Link did not initialize. Try again.')); }
    };
    script.onerror = () => {
      sdk = undefined;
      script.remove();
      reject(new Error('Plaid Link did not load. Check your connection and try again.'));
    };
    document.head.append(script);
  });
  return sdk;
}

function save(session: LinkSession): void {
  sessionStorage.setItem(storageKey, JSON.stringify(session));
}

export function clearSession(): void {
  sessionStorage.removeItem(storageKey);
  sessionStorage.removeItem(completionKey);
}

function retireCallback(): void {
  if (window.location.pathname === '/oauth-return') history.replaceState(null, '', '/');
}

export function hasPendingCompletion(): boolean {
  return sessionStorage.getItem(completionKey) !== null;
}

export async function finishPendingConnection(): Promise<void> {
  const saved = sessionStorage.getItem(completionKey);
  if (!saved) throw new Error('There is no connection waiting to finish.');
  let completion: Finish;
  try { completion = JSON.parse(saved) as Finish; }
  catch { throw new Error('Saved connection data cannot be read. Check your connected institutions before starting again.'); }
  await completeSession(completion);
  clearSession();
  retireCallback();
}

export function hasOAuthReturn(): boolean {
  return window.location.pathname === '/oauth-return' && new URLSearchParams(window.location.search).has('oauth_state_id');
}

export async function openLink(connectionId?: string, resume = false): Promise<'connected' | 'cancelled'> {
  if (!resume) {
    clearSession();
    retireCallback();
  }
  const plaid = await loadSdk();
  let session: LinkSession;
  if (resume) {
    const stored = sessionStorage.getItem(storageKey);
    if (!stored) throw new Error('This browser has no pending connection. Start again from Accounts.');
    session = JSON.parse(stored) as LinkSession;
    if (Date.parse(session.expiration) <= Date.now()) {
      clearSession();
      throw new Error('The connection session expired. Start again from Accounts.');
    }
  } else {
    session = await createSession(connectionId);
    save(session);
  }
  return new Promise<'connected' | 'cancelled'>((resolve, reject) => {
    let completing = false;
    const handler = plaid.create({
      token: session.link_token,
      ...(resume ? { receivedRedirectUri: window.location.href } : {}),
      onSuccess: async (publicToken, metadata) => {
        if (completing) return;
        completing = true;
        try {
          const completion: Finish = {
            session_id: session.id,
            public_token: session.mode === 'connect' ? publicToken : null,
            institution_name: metadata.institution?.name ?? null,
          };
          sessionStorage.setItem(completionKey, JSON.stringify(completion));
          await finishPendingConnection();
          handler.destroy();
          resolve('connected');
        } catch (error) {
          handler.destroy();
          reject(error);
        }
      },
      onExit: (error) => {
        if (completing) return;
        clearSession();
        retireCallback();
        handler.destroy();
        if (error) reject(new Error(error.display_message ?? 'The bank connection could not complete. Try again.'));
        else resolve('cancelled');
      },
    });
    handler.open();
  });
}
