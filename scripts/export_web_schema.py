"""Export browser API contracts without a running service or live credentials."""

import json
import tempfile
from pathlib import Path

from plaid_core.application import initialize_application
from plaid_core.config import Config
from plaid_link.server import create_app

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="plaid-schema-") as temporary:
        root = Path(temporary)
        initialize_application(root, Config())
        schema = create_app(root, setup_token="schema-only-unused-token").openapi()
    destination = Path(__file__).resolve().parents[1] / "packages/plaid-web/schema.json"
    destination.write_text(json.dumps(schema, indent=2) + "\n")
