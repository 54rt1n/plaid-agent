import json
import os
import selectors
import signal
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from plaid_core.config import LinkSettings, update_config
from plaid_core.errors import FinanceError
from plaid_core.link import Link
from plaid_link import connect as connection_command
from plaid_link.server import create_app


@pytest.fixture
def listener(app, fake, tmp_path, monkeypatch):
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("OAUTH_CALLBACK_URI", origin + "/oauth-return")
    monkeypatch.setenv("PLAID_CLIENT_ID", "test-client")
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "test-sandbox-secret")
    update_config(tmp_path, app.config.model_copy(update={"link_port": port}))
    monkeypatch.setattr(
        connection_command,
        "create_app",
        lambda *args, **kwargs: create_app(*args, **kwargs, transport=httpx.MockTransport(fake)),
    )
    return origin, port


def complete_in_browser(url, *, connection_id=None):
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    token = parse_qs(parsed.fragment)["setup"][0]
    with httpx.Client(base_url=origin, headers={"origin": origin}, trust_env=False) as browser:
        assert browser.post("/api/session", json={"token": token}).status_code == 204
        response = browser.post("/api/link/sessions", json={"connection_id": connection_id})
        assert response.status_code == 200, response.text
        session = response.json()
        # Rejected completion must leave the flow running for another attempt.
        if connection_id is None:
            assert (
                browser.post("/api/link/complete", json={"session_id": session["id"]}).status_code
                == 400
            )
        response = browser.post(
            "/api/link/complete",
            json={
                "session_id": session["id"],
                "public_token": "test-public",
                "institution_name": "Test bank",
            },
        )
        assert response.status_code == 200, response.text
        replay = browser.post(
            "/api/link/complete", json={"session_id": session["id"], "public_token": "test-public"}
        )
        assert replay.status_code == 200 and replay.json() == response.json()
        return response.json()


def finish_setup(url):
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    with httpx.Client(base_url=origin, headers={"origin": origin}, trust_env=False) as browser:
        token = parse_qs(parsed.fragment)["setup"][0]
        assert browser.post("/api/session", json={"token": token}).status_code == 204
        assert browser.post("/api/setup/finish", json={}).status_code == 204


def test_connect_keeps_multiple_connections_open_until_user_finishes(
    app, listener, tmp_path, monkeypatch
):
    origin, port = listener

    def open_browser(url):
        assert url.startswith(origin + "/#setup=")
        assert (tmp_path / "link-server.json").stat().st_mode & 0o077 == 0
        first = complete_in_browser(url)
        # Saving and retrying a connection must leave the setup website available.
        assert httpx.get(origin, trust_env=False).status_code == 200
        second = complete_in_browser(url)
        assert first["id"] != second["id"]
        complete_in_browser(url, connection_id=first["id"])
        assert len(app.list_connections()) == 2
        finish_setup(url)
        return True

    monkeypatch.setattr(connection_command.webbrowser, "open", open_browser)
    result = connection_command.connect(tmp_path)
    assert len(result) == 2
    assert {item.id for item in app.list_connections()} == {item.id for item in result}
    assert not (tmp_path / "link-server.json").exists()
    with socket.socket() as freed:
        freed.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        freed.bind(("127.0.0.1", port))


@pytest.mark.parametrize("renewal", [False, True])
def test_cli_connect_reuses_existing_server_and_leaves_it_running(app, listener, tmp_path, renewal):
    origin, port = listener
    identity = Link(app).sandbox_connection("ins_test").id if renewal else None
    with connection_command.running_server(
        tmp_path, env_file=None, host="127.0.0.1", port=port
    ) as (runtime, server, thread):
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "plaid_cli",
                "--data-dir",
                str(tmp_path),
                "--json",
                "connect",
                "--no-open",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stderr.readline().strip() == "Connect an institution in your browser:"
            private_url = process.stderr.readline().strip()
            assert private_url == runtime.url
            saved = complete_in_browser(private_url, connection_id=identity)
            assert process.poll() is None
            finish_setup(private_url)
            output, error = process.communicate(timeout=10)
            assert process.returncode == 0, error
            assert saved["id"] in {item["id"] for item in json.loads(output)}
            assert thread.is_alive() and not server.should_exit
            assert (tmp_path / "link-server.json").exists()
            assert httpx.get(origin, trust_env=False).status_code == 200
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_cancel_cleans_up_temporary_listener_without_creating_connection(
    app, listener, tmp_path, monkeypatch
):
    def cancelled(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(connection_command, "_open", cancelled)
    with pytest.raises(FinanceError, match="temporary server stopped"):
        connection_command.connect(tmp_path)
    assert app.list_connections() == []
    assert not (tmp_path / "link-server.json").exists()
    with socket.socket() as freed:
        freed.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        freed.bind(("127.0.0.1", listener[1]))


def test_stale_previous_runtime_does_not_block_existing_connection(app, listener, tmp_path):
    saved = Link(app).sandbox_connection("ins_test")
    origin, port = listener
    path = tmp_path / "link-server.json"
    path.write_text(
        json.dumps(
            {
                "origin": origin,
                "host": "127.0.0.1",
                "port": port,
                "environment": "sandbox",
                "token": "old-secret",
                "completed_session": "old-session",
                "connection_id": saved.id,
            }
        )
    )
    path.chmod(0o600)
    assert (
        connection_command._existing(
            tmp_path, app.config, LinkSettings(callback_uri=origin + "/oauth-return")
        )
        is None
    )
    assert not path.exists()
    assert app.list_connections() == [saved]


def test_installed_connect_emits_ready_url_without_pty_and_interrupts_cleanly(
    app, listener, tmp_path
):
    origin, port = listener
    command = Path(sys.executable).parent / "plaid-agent"
    process = subprocess.Popen(
        [str(command), "--data-dir", str(tmp_path), "connect", "--no-open"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            assert selector.select(timeout=10), "No startup output without a PTY"
            assert process.stderr.readline().strip() == "Connect an institution in your browser:"
            private_url = process.stderr.readline().strip()
        assert private_url.startswith(origin + "/#setup=")
        assert process.poll() is None
        assert httpx.get(origin, trust_env=False).status_code == 200
        process.send_signal(signal.SIGINT)
        _, error = process.communicate(timeout=10)
        assert process.returncode == 1 and "temporary server stopped" in error
        assert not (tmp_path / "link-server.json").exists()
        with socket.socket() as freed:
            freed.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            freed.bind(("127.0.0.1", port))
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
