# Plaid Agent

Connect financial institutions, cache their transactions and balances, and query
that data from the CLI or an agent. The Python library is shared by the CLI,
MCP server and account-connection website.

[Install](#install) · [Connect accounts](#connect-accounts) · [Use the CLI](#use-the-cli) ·
[Storage](#storage) · [Development](#development)

## Install

Install Python 3.11–3.13, Git, uv, Node 22.12+ and npm 9.5+, then download the project:

```bash
git clone https://github.com/54rt1n/plaid-agent.git
cd plaid-agent
uv sync --python 3.12
uv run plaid-agent init
uv run plaid-agent status
```

`init` creates a **sandbox** configuration. Run it once per data directory;
it refuses to overwrite an existing configuration.

Data defaults to `$XDG_DATA_HOME/plaid-agent`, or `~/.local/share/plaid-agent`.
Use `--data-dir PATH` before a command to select another directory. All examples
below run from the checkout using `uv run`.

For Hermes, use the separate `hermes-plaid-agent` plugin. Its installation guide
covers profile storage, the bundled skill and MCP activation. Its installed CLI
accepts `--profile NAME` to select that profile's data.

## Connect accounts

Choose sandbox for test data or production for real accounts. Each environment
has a separate database and encryption key.

### Sandbox

Create a private `.env` file with your Plaid sandbox credentials:

```dotenv
PLAID_CLIENT_ID=your-client-id
PLAID_SANDBOX_SECRET=your-sandbox-secret
```

```bash
chmod 600 .env
uv run plaid-agent --env-file .env connections create-sandbox
```

Use the returned connection ID to load its data:

```bash
uv run plaid-agent --env-file .env transactions sync CONNECTION_ID
uv run plaid-agent accounts list
uv run plaid-agent transactions list
```

Initial history loads asynchronously. An empty first sync does not prove that
there are no transactions. To use the browser-based sandbox flow instead, run
`uv run plaid-agent --env-file .env connect`; its default callback is
`http://127.0.0.1:8765/oauth-return`. Register that exact URI in the Plaid Dashboard.

### Real accounts

Your Plaid application needs production access to the products you use. Add your
production secret and registered callback URI to `.env`:

```dotenv
PLAID_CLIENT_SECRET=your-production-secret
OAUTH_CALLBACK_URI=https://localhost:8756/oauth-return
```

This example assumes the browser runs on the same machine. For another device,
replace `localhost` with a hostname that device can reach. Register the resulting
exact URI in your Plaid Dashboard.

Production callbacks require HTTPS. Supply a PEM certificate chain and its
matching unencrypted PEM private key. The certificate must cover the callback
hostname and be trusted by the browser. Protect the key file with private
permissions.

```bash
uv run plaid-agent config set --environment production --link-port 8756 \
  --link-certfile /path/to/certificate.pem --link-keyfile /path/to/private-key.pem
uv run plaid-agent --env-file .env connect
```

The application serves the website and API directly over HTTPS on that port.
These settings also apply to `link serve`. To choose certificates for one
invocation, pass both `--certfile` and `--keyfile` to either command.

`connect` starts a temporary backend and opens the setup page. Add institutions
or renew access as needed, then choose **Finish setup**. Each saved connection
returns to the same page.

For a browser on another device, use:

```bash
uv run plaid-agent --env-file .env connect --host 0.0.0.0 --no-open
```

The command prints a private setup URL to stderr and waits for Finish setup.
That URL grants setup access; share it only with the operator. Ctrl+C stops a
backend started by this command and keeps saved connections. An existing backend
for the same data directory is reused and left running.

After connecting, load transaction data explicitly:

```bash
uv run plaid-agent --env-file .env transactions sync --all
```

For a persistent website, use `uv run plaid-agent --env-file .env link serve --open`.
Both commands accept `--host` and `--port`. A reverse proxy is optional; an HTTP
listener remains available when no certificate/key pair is configured.
Restart the backend after changing its environment or callback configuration.

The [Plaid Link SDK](https://plaid.com/docs/link/web/) must load from Plaid's CDN. Vite bundles the application;
one Python process serves the compiled website and API. Bank credentials stay
with Plaid and the institution; client secrets and token exchange stay in Python.

## Use the CLI

Human-readable output is the default. `--json`, `--env-file`, `--data-dir` and
`--profile` are global options and go before the command.

Credentials are read from an explicit `--env-file` or, when present, `.env` in the
data directory. Inherited environment variables take precedence. Sandbox uses
`PLAID_SANDBOX_SECRET`; production uses `PLAID_CLIENT_SECRET`.

### Accounts and transactions

```bash
uv run plaid-agent status
uv run plaid-agent connections list
uv run plaid-agent accounts list
uv run plaid-agent --env-file .env transactions sync CONNECTION_ID
uv run plaid-agent --env-file .env transactions sync --all
uv run plaid-agent transactions list --account ACCOUNT_ID --direction credit
uv run plaid-agent transactions list --text coffee --start 2026-01-01 --end 2026-01-31
uv run plaid-agent transactions list --category FOOD_AND_DRINK --include-pending
uv run plaid-agent transactions summary --start 2026-01-01 --end 2026-01-31 --group-by account
uv run plaid-agent transactions coverage --group-by account
```

Queries read the local cache without calling Plaid. Sync applies added, modified
and removed transactions atomically with the connection's cursor. `--all` syncs
connections with transaction access and reports each success, failure or skip.
Partial failure preserves successful changes and returns a nonzero exit status.
It does not refresh other products or request real-time balances.

Positive Plaid amounts are debits; negative amounts are credits. Reports keep
currencies separate and exclude pending transactions by default. Credits can be
refunds or transfers, and debits can be payments or transfers, so gross cashflow
is not an income/expense classification.

`transactions coverage` reports stored counts and earliest/latest dates without
returning transaction details. It excludes pending bank records unless
`--include-pending` is set. Stored ranges do not prove complete institution history.
`status` shows whether a connection has cached records or has not yet been synced.

Account output separates when the application fetched Plaid's observation from
when the provider last updated the balance, if supplied. A recent fetch or
transaction sync does not establish a real-time bank balance.

### Investments and snapshots

Enable products when initializing a fresh data directory, for example:
`uv run plaid-agent init --product transactions --product investments --product liabilities`.
Changing configuration does not add product consent to existing connections.

```bash
uv run plaid-agent --env-file .env accounts refresh CONNECTION_ID
uv run plaid-agent --env-file .env investments holdings refresh CONNECTION_ID
uv run plaid-agent --env-file .env investments transactions refresh CONNECTION_ID \
  --start 2026-01-01 --end 2026-01-31
uv run plaid-agent --env-file .env liabilities refresh CONNECTION_ID
uv run plaid-agent investments holdings list
uv run plaid-agent liabilities list
uv run plaid-agent transactions list --kind investment
uv run plaid-agent transactions summary --kind investment --start 2026-01-01 --end 2026-01-31
uv run plaid-agent snapshots list --kind balances
uv run plaid-agent snapshots show SNAPSHOT_ID
```

Balance, holdings and liability refreshes retain timestamped snapshots.
Investment transaction refresh replaces only the requested date range after all
pages succeed. Use `--kind investment` to query those records; bank is the default.
`accounts refresh CONNECTION_ID --realtime` requests a potentially billable
real-time balance extraction.

### JSON and source details

```bash
uv run plaid-agent --json transactions list --limit 100 --offset 100
uv run plaid-agent --json transactions list --text coffee --include-details
uv run plaid-agent --json snapshots show SNAPSHOT_ID --include-details
```

Paged results include `total` and `next_offset`. `--include-details` requires JSON
output and returns the complete original provider payload. Unknown provider fields
are retained in encrypted storage.

### Configuration and removal

```bash
uv run plaid-agent config show
uv run plaid-agent config set --history-days 730
uv run plaid-agent --env-file .env connections remove CONNECTION_ID --confirm
```

Removal disconnects the institution in Plaid and deletes its local cached records
and snapshots.

## Storage

```text
DATA_DIR/
  config.toml
  .env                 # Optional private credentials
  sandbox/
    finance.sqlite3
    encryption.key
  production/
    finance.sqlite3
    encryption.key
```

The SQLite database stores connections, accounts, transactions, snapshots and
Link sessions. Financial payloads and access tokens are encrypted fields;
identifiers, cursors and snapshot fetch times are operational metadata. The
whole database is not SQLCipher-encrypted.

Back up each database with its encryption key. Losing the key makes encrypted
records unreadable; possession of both permits decryption. Data returned to an
agent is subject to its model-provider and conversation-retention policies.

## Development

| Package | Responsibility |
| --- | --- |
| `plaid-core` | Typed contracts, provider client, storage and application |
| `plaid-cli` | Management commands and human/JSON output |
| `plaid-link` | HTTP backend and compiled website |
| `plaid-mcp` | Thirteen financial tools over stdio |
| `plaid-web` | Vite, Svelte and TypeScript frontend |

Python packages use `packages/<package>/src/<module>`. `uv sync` installs them
editable and builds the website during installation. Wheels include the compiled
website; installing a wheel does not require Node. For frontend changes:

```bash
cd packages/plaid-web
npm ci
npm run generate:api
npm run build
```

`npm run dev` watches and rebuilds files served by the Python backend. Compiled
files are ignored by Git. Package builds run `npm ci` and `npm run build`, then
include the result in the wheel. Source distributions carry the frontend source
and lockfile so they can build independently of the repository checkout.

From the repository root:

```bash
bash scripts/run_tests.sh -q
uv run ruff check .
uv run ruff format --check .
```

Browser tests use a temporary backend, fake Plaid responses and an intercepted SDK:

```bash
cd packages/plaid-web
npx playwright install chromium
npm test
```

Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to use an installed Chromium instead.
Architecture and package decisions are in the [design](docs/design/PLAID_AGENT_DESIGN.md).

## License

[PolyForm Internal Use License 1.0.0](LICENSE). Bundled frontend components retain
their [original licenses](packages/plaid-link/THIRD_PARTY_NOTICES).
