# Plaid Agent

Build a reusable personal-finance application and a separate Hermes plugin. This
is an original implementation against Plaid's documented API; no code from
`austinbv/hermes-plaid` is included. Schedule Agent's installed package boundaries
and Hermes Schedule Agent's portable manifest are the integration references.

## Decisions and ownership

- **D001 — Application packages.** `packages/plaid-core/src/plaid_core`
  owns validated Pydantic contracts, Plaid requests, encrypted SQLite persistence,
  connection lifecycle, synchronization, and reporting. `plaid-cli`, `plaid-link`,
  and `plaid-mcp` depend on core; core imports none of them or Hermes.
  `packages/plaid-web` is the Vite/Svelte/TypeScript browser application. Its
  assets are compiled during package builds and included in the `plaid-link` wheel,
  not versioned in Git; the Python HTTP backend
  serves them and calls core. No HTML application is embedded in Python. Packages
  contain modules under `src`; there is no installable workspace root.
- **D002 — Separate Hermes repository.** `hermes-plaid-agent` contains a portable
  MCP manifest, launcher, runtime installer, profile adapter, and execution skill.
  It consumes application packages as dependencies and never edits Hermes core.
  Hermes manages stdio MCP processes. Installing tools takes effect in a new
  session or through Hermes' explicit MCP reload; no mid-turn prompt mutation.
- **D003 — Owned local state.** Standalone state defaults to
  `$XDG_DATA_HOME/plaid-agent` (or `~/.local/share/plaid-agent`), overridable by
  `--data-dir`. The plugin always uses absolute Hermes-provided `PLUGIN_DATA`.
  `--profile` in the management CLI is resolved by the optional Hermes adapter,
  through Hermes' profile command. Each root contains `config.toml`; sandbox and
  production use separate subdirectories, databases, and encryption keys.
  Secrets come from `PLAID_CLIENT_ID`, `PLAID_SANDBOX_SECRET` (sandbox), and
  `PLAID_CLIENT_SECRET` (production), never config or chat. An explicit `--env-file`
  may load an operator-provided dotenv file without overwriting inherited values.
- **D004 — Provider data and public DTOs differ.** Access tokens, complete original
  provider transaction/snapshot payloads, and normalized DTOs are Fernet encrypted.
  Public queries expose normalized records; complete details are an explicit query
  option. Connection IDs, account IDs, cursor, environment, and fetch
  timestamps are operational SQLite metadata. This is field encryption, not an
  encrypted SQLite engine. The key is a private local file alongside the database;
  host compromise with access to both defeats this protection. Public account,
  transaction, investment, and liability summaries exclude account/routing numbers,
  addresses, credentials, and tokens. Model-provider/chat retention still applies
  to DTOs an agent reads. Connection creation and key provisioning happen only in
  management flows, never as a side effect of reading uninitialized state.
- **D005 — Atomic synchronization.** Collect every page before committing added,
  modified, removed records and the final cursor in one SQLite transaction. A
  Plaid pagination-mutation error discards that attempt and restarts from the
  original cursor with a bounded retry. An optimistic cursor check rejects a
  concurrent stale writer. Failed fetches leave prior records and cursor intact.
- **D006 — Financial register over a cache.** Money uses finite `Decimal` values.
  Totals are separated by ISO/unofficial currency. Pending transactions are
  excluded by default. Plaid's signed amount is retained: positive means a debit
  (cash outflow/charge), negative means a credit (cash inflow/refund/payment).
  Typed DTOs provide nonnegative `debit` and `credit` values and a direction filter.
  This uses the provider transaction convention, not double-entry accounting.
  Reports describe gross credits and debits, with no
  automatic income/expense/net-worth inference or hidden transfer matching.
  Cached queries identify fetch timestamps; they cannot promise bank freshness.
- **D007 — Link website.** The Svelte website calls a separate ASGI backend that
  serves its compiled assets and creates initial/update Link tokens, exchanges initial public tokens, and
  completes reauthorization without replacing an existing access token. A
  registered `OAUTH_CALLBACK_URI` from the private `.env` identifies the browser-facing
  site and its `/oauth-return` route, independently of the listener's host and port.
  Production uses HTTPS, including for localhost. The listener can load a PEM
  certificate/key pair trusted by the browser; a reverse proxy is optional.
  No public endpoint is required.
  An operator-generated setup token authenticates
  browser sessions; it enters via URL fragment, becomes an HttpOnly SameSite
  cookie, and is removed from browser history. Mutating APIs require exact Origin
  validation. There is no public webhook receiver in this implementation.
  Plaid's configured `client_name` is `Personal Plaid Agent`.
  Each saved session retains its requested products. Local credentials are
  validated before taking a durable completion claim. Connection insertion and
  the completed result commit together; repeated callbacks return that stored
  connection without another exchange. An uncertain provider outcome keeps the
  claim and is reported explicitly rather than blindly repeating the exchange.
  Browser completion payloads survive lost responses and reloads; terminal exits
  and fresh flows retire obsolete OAuth callback URLs. Configured origins are
  canonicalized to match browser scheme/host/default-port behavior.
  `connect` owns a temporary listener and opens the authenticated browser URL.
  Saving connections or renewing access leaves setup open. Only the explicit
  authenticated **Finish setup** action ends the command and its listener; the
  command returns the configured connections. A reused management server remains
  running. Closing setup never removes saved connections.
- **D008 — CLI owns management.** Init/configuration, Link launch, sandbox
  creation, disconnect, status, sync, and all reads have ordinary flags. `--json`
  produces DTO JSON; human output is separate. Thirteen MCP tools expose bounded
  financial reads and explicit refresh/sync. They expose no tokens, linking,
  disconnect, configuration, or money movement. CLI and MCP call the same methods.
- **D009 — Dependencies and delivery.** The development workflow supports editable
  installs. Normal plugin setup consumes a release requirements file containing
  immutable GitHub revisions; it must fail clearly until a real published
  revision exists. Never invent a pin or claim a local checkout is a release
  prerequisite. Publication and live account linking are outside this task.
- **D010 — Dedicated financial tables.** `transactions` stores the current cache,
  keyed by `(connection_id, source_kind, transaction_id)`, with account identity,
  encrypted typed record and encrypted complete provider payload. `snapshots`
  appends timestamped balance/holdings/liability observations, each with a typed
  projection and complete encrypted provider response. `accounts` is the current
  account projection, retaining metadata when an account becomes inactive.
  `connections` owns tokens and cursors; `link_sessions` owns temporary Link state.
  A snapshot's fetch time means when we retrieved Plaid's observation, not when
  the bank last changed its balance. Do not derive a running balance without a
  known anchor and complete transaction history. User confirmed a cache is
  appropriate; no journals, chart of accounts, or immutable accounting ledger.
- **D011 — Shared bank/investment register.** Investment refresh collects a whole
  requested date range before replacing that range atomically in `transactions`.
  Investment security, quantity, fee, price and type fields live in the typed
  transaction's investment details; the original payload is retained. General
  transaction queries filter by `source_kind` (bank by default). A bank cursor
  update cannot remove investment transactions. Different source kinds may reuse
  a provider transaction ID safely.

## Invariants

- **INV-001:** No Hermes import or transport dependency in core.
- **INV-002:** A failed sync, stale cursor, or pagination retry cannot partially
  alter the visible transaction register or advance its cursor.
- **INV-003:** Different roots/environments cannot read each other's connection
  tokens or cache, and public DTOs/errors cannot contain provider secrets.
- **INV-004:** A report never sums different currencies or silently treats pending
  transactions as posted income/expense.
- **INV-005:** Unauthenticated or cross-origin requests cannot create Link tokens,
  exchange public tokens, or complete a reconnection.
- **INV-006:** Installed wheels include Link assets and work outside source roots;
  CLI/MCP use shared validated models and real durable storage.

## References

Plaid API references: [Link](https://plaid.com/docs/api/link/),
[update mode](https://plaid.com/docs/link/update-mode/),
[Transactions](https://plaid.com/docs/api/products/transactions/),
[Accounts](https://plaid.com/docs/api/accounts/),
[Investments](https://plaid.com/docs/api/products/investments/), and
[Liabilities](https://plaid.com/docs/api/products/liabilities/).
