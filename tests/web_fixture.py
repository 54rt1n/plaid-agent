"""Temporary fake-Plaid backend for real browser integration tests."""

from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import uvicorn
from conftest import PlaidFixture
from plaid_core.application import initialize_application
from plaid_core.config import Config
from plaid_link.server import create_app

if __name__ == "__main__":
    with TemporaryDirectory(prefix="plaid-browser-") as temporary:
        root = Path(temporary)
        initialize_application(root, Config())
        credentials = root / ".env"
        credentials.write_text(
            "PLAID_CLIENT_ID=test-client\nPLAID_SANDBOX_SECRET=test-sandbox-secret\n"
            "OAUTH_CALLBACK_URI=http://127.0.0.1:8797/oauth-return\n"
        )
        credentials.chmod(0o600)
        app = create_app(
            root,
            setup_token="browser-test-setup",
            env_file=credentials,
            transport=httpx.MockTransport(PlaidFixture()),
        )
        uvicorn.run(app, host="127.0.0.1", port=8797, log_level="warning", access_log=False)
