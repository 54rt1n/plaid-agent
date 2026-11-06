import type { components } from './api.generated';

export type Status = components['schemas']['Status'];
export type Connection = components['schemas']['Connection'];
export type LinkSession = components['schemas']['LinkSession'];
export type Finish = components['schemas']['Finish'];

export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 204) return undefined as T;
  const result = await response.json();
  if (!response.ok) {
    throw new ApiError(result.error ?? result.detail ?? 'The request could not complete.', response.status);
  }
  return result as T;
}

export const signIn = (token: string) => request<void>('/api/session', 'POST', { token });
export const signOut = () => request<void>('/api/session', 'DELETE');
export const getStatus = () => request<Status>('/api/status');
export const createSession = (connectionId?: string) =>
  request<LinkSession>('/api/link/sessions', 'POST', { connection_id: connectionId ?? null });
export const completeSession = (body: Finish) => request<Connection>('/api/link/complete', 'POST', body);
export const finishSetup = () => request<void>('/api/setup/finish', 'POST', {});
