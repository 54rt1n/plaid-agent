from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
import pytest_asyncio
from plaid_core.application import Finance
from plaid_core.config import Config, LinkSettings, update_config
from plaid_core.errors import FinanceError, ProviderError
from plaid_core.link import Link
from plaid_core.models import LinkState
from plaid_link.server import create_app

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def web(app, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "test-client")
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "test-sandbox-secret")
    server = create_app(
        tmp_path, setup_token="operator-setup-token", transport=httpx.MockTransport(fake)
    )
    async with server.router.lifespan_context(server):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server), base_url="http://127.0.0.1:8765"
        ) as client:
            yield client


async def login(web):
    result = await web.post(
        "/api/session",
        json={"token": "operator-setup-token"},
        headers={"origin": "http://127.0.0.1:8765"},
    )
    assert result.status_code == 204
    assert "httponly" in result.headers["set-cookie"].lower()


async def test_authentication_and_origin_protect_financial_routes(web):
    assert (await web.get("/api/status")).status_code == 401
    assert (
        await web.post("/api/session", json={"token": "operator-setup-token"})
    ).status_code == 403
    await login(web)
    assert (await web.get("/api/status")).status_code == 200
    invalid = await web.post(
        "/api/session",
        json={"token": "🔒 invalid token"},
        headers={"origin": "http://127.0.0.1:8765"},
    )
    assert invalid.status_code == 401
    result = await web.post(
        "/api/link/sessions", json={}, headers={"origin": "https://attacker.test"}
    )
    assert result.status_code == 403
    rejected = await web.post(
        "/api/link/complete",
        json={"unknown": "sensitive-public-token"},
        headers={"origin": "http://127.0.0.1:8765"},
    )
    assert rejected.status_code == 422
    assert "sensitive-public-token" not in rejected.text


async def test_initial_and_update_link_flows_share_durable_storage(web, fake, tmp_path):
    await login(web)
    origin = {"origin": "http://127.0.0.1:8765"}
    session = (await web.post("/api/link/sessions", json={}, headers=origin)).json()
    assert session["mode"] == "connect"
    assert session["link_token"] == "test-link-token"
    assert [body for path, body in fake.calls if path == "/link/token/create"][-1][
        "client_name"
    ] == "Personal Plaid Agent"
    result = await web.post(
        "/api/link/complete",
        json={"session_id": session["id"], "public_token": "test-public"},
        headers=origin,
    )
    assert result.status_code == 200, result.text
    assert "test-access-token" not in result.text
    identity = result.json()["id"]
    update = (
        await web.post("/api/link/sessions", json={"connection_id": identity}, headers=origin)
    ).json()
    assert update["mode"] == "update"
    request = [body for path, body in fake.calls if path == "/link/token/create"][-1]
    assert request["access_token"] == "test-access-token"
    assert "products" not in request
    updated = await web.post(
        "/api/link/complete", json={"session_id": update["id"]}, headers=origin
    )
    assert updated.status_code == 200
    assert len([p for p, _ in fake.calls if p == "/item/public_token/exchange"]) == 1
    with Finance(tmp_path) as reopened:
        assert reopened.store.connection(identity).token == "test-access-token"
    replay = await web.post(
        "/api/link/complete",
        json={"session_id": session["id"], "public_token": "test-public"},
        headers=origin,
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == identity
    assert len([p for p, _ in fake.calls if p == "/item/public_token/exchange"]) == 1


async def test_pending_link_survives_application_reopen(app, tmp_path):
    session = Link(app).create_session()
    with Finance(tmp_path, client=app.client) as reopened:
        connection = Link(reopened).complete(session.id, "test-public")
    assert connection.id == "connection-one"


async def test_pending_link_preserves_requested_products_after_config_change(app, tmp_path, fake):
    requested = app.config.products
    session = Link(app).create_session()
    update_config(tmp_path, Config(products=("investments",)))
    with Finance(tmp_path, client=app.client) as reopened:
        connection = Link(reopened).complete(session.id, "test-public")
    assert connection.products == requested


async def test_link_completion_is_serialized_and_replay_survives_restart(
    app, fake, tmp_path, monkeypatch
):
    session = Link(app).create_session()
    entered, release = Event(), Event()
    original = app.client.post

    def blocked_exchange(endpoint, payload):
        if endpoint == "/item/public_token/exchange":
            entered.set()
            assert release.wait(5)
        return original(endpoint, payload)

    monkeypatch.setattr(app.client, "post", blocked_exchange)
    with ThreadPoolExecutor(max_workers=2) as threads:
        first = threads.submit(Link(app).complete, session.id, "test-public")
        try:
            assert entered.wait(3)
            duplicate = threads.submit(Link(app).complete, session.id, "test-public")
            with pytest.raises(FinanceError, match="in progress"):
                duplicate.result(timeout=3)
        finally:
            release.set()
        result = first.result(timeout=3)
    with Finance(tmp_path) as reopened:
        assert Link(reopened).complete(session.id) == result
    assert len([path for path, _ in fake.calls if path == "/item/public_token/exchange"]) == 1


async def test_failed_local_commit_does_not_repeat_exchange(app, fake, monkeypatch):
    session = Link(app).create_session()
    original_seal = app.store.seal

    def fail_outcome(value):
        if isinstance(value, LinkState) and value.status == "completed":
            raise FinanceError("Injected outcome storage failure")
        return original_seal(value)

    monkeypatch.setattr(app.store, "seal", fail_outcome)
    with pytest.raises(FinanceError, match="storage failure"):
        Link(app).complete(session.id, "test-public")
    assert app.list_connections() == []
    with pytest.raises(FinanceError, match="outcome is unknown"):
        Link(app).complete(session.id, "test-public")
    assert len([path for path, _ in fake.calls if path == "/item/public_token/exchange"]) == 1


async def test_missing_local_credentials_does_not_consume_completion(
    app, fake, tmp_path, monkeypatch
):
    session = Link(app).create_session()
    for name in ("PLAID_CLIENT_ID", "PLAID_SANDBOX_SECRET", "PLAID_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with Finance(tmp_path) as missing_credentials:
        with pytest.raises(FinanceError, match="Set PLAID_CLIENT_ID"):
            Link(missing_credentials).complete(session.id, "test-public")
    assert not [path for path, _ in fake.calls if path == "/item/public_token/exchange"]
    with Finance(tmp_path, client=app.client) as configured:
        result = Link(configured).complete(session.id, "test-public")
    assert result.id == "connection-one"


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadError("connection interrupted"),
        httpx.Response(500, json={"error_code": "INTERNAL_SERVER_ERROR"}),
    ],
)
async def test_uncertain_exchange_result_is_not_reissued(app, fake, failure, tmp_path):
    session = Link(app).create_session()
    fake.exchange_failure = failure
    with pytest.raises(ProviderError) as caught:
        Link(app).complete(session.id, "test-public")
    assert caught.value.outcome_unknown
    with Finance(tmp_path) as reopened:
        with pytest.raises(FinanceError, match="outcome is unknown"):
            Link(reopened).complete(session.id, "test-public")
    assert len([path for path, _ in fake.calls if path == "/item/public_token/exchange"]) == 1


@pytest.mark.parametrize(
    ("configured", "browser_origin"),
    [
        ("http://127.0.0.1:80", "http://127.0.0.1"),
        ("https://FINANCE.EXAMPLE:443", "https://finance.example"),
        ("http://[0:0:0:0:0:0:0:1]:80", "http://[::1]"),
    ],
)
async def test_origin_configuration_matches_browser_canonicalization(
    app, tmp_path, configured, browser_origin, monkeypatch
):
    monkeypatch.setenv("OAUTH_CALLBACK_URI", configured + "/oauth-return")
    server = create_app(tmp_path, setup_token="operator-setup-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url=browser_origin
    ) as client:
        result = await client.post(
            "/api/session",
            json={"token": "operator-setup-token"},
            headers={"origin": browser_origin},
        )
        assert result.status_code == 204, result.text


async def test_website_and_oauth_return_are_served_by_the_same_application(web):
    for path in ["/", "/oauth-return?oauth_state_id=test"]:
        response = await web.get(path)
        assert response.status_code == 200, response.text
        assert 'id="app"' in response.text
        assert "https://cdn.plaid.com" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_profile_callback_drives_https_cookie_origin_and_provider_request(
    app, fake, tmp_path
):
    fake.expected_secret = "test-production-secret"
    update_config(tmp_path, Config(environment="production"))
    credentials = tmp_path / ".env"
    credentials.write_text(
        "PLAID_CLIENT_ID=test-client\nPLAID_CLIENT_SECRET=test-production-secret\n"
        "OAUTH_CALLBACK_URI=https://link.example.test:8756/oauth-return\n"
    )
    credentials.chmod(0o600)
    server = create_app(tmp_path, setup_token="operator", transport=httpx.MockTransport(fake))
    origin = "https://link.example.test:8756"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url=origin
    ) as client:
        response = await client.post(
            "/api/session", json={"token": "operator"}, headers={"origin": origin}
        )
        assert response.status_code == 204
        assert "; Secure" in response.headers["set-cookie"]
        assert (await client.get("/oauth-return?oauth_state_id=test")).status_code == 200
        response = await client.post("/api/link/sessions", json={}, headers={"origin": origin})
        assert response.status_code == 200, response.text
        request = [body for path, body in fake.calls if path == "/link/token/create"][-1]
        assert request["redirect_uri"] == origin + "/oauth-return"
        assert (
            await client.post(
                "/api/link/sessions", json={}, headers={"origin": "http://link.example.test:8756"}
            )
        ).status_code == 403
        assert (await client.get("/", headers={"host": "attacker.test"})).status_code == 400


async def test_callback_environment_precedence_and_production_validation(tmp_path, monkeypatch):
    monkeypatch.delenv("OAUTH_CALLBACK_URI", raising=False)
    with pytest.raises(FinanceError, match="Set OAUTH_CALLBACK_URI"):
        LinkSettings.from_environment("production")
    credentials = tmp_path / ".env"
    credentials.write_text("OAUTH_CALLBACK_URI=https://link.example.test:8756/oauth-return\n")
    credentials.chmod(0o600)
    assert (
        LinkSettings.from_environment("production", credentials).origin
        == "https://link.example.test:8756"
    )
    monkeypatch.setenv("OAUTH_CALLBACK_URI", "https://localhost:8756/oauth-return")
    assert (
        LinkSettings.from_environment("production", credentials).origin == "https://localhost:8756"
    )
    monkeypatch.setenv("OAUTH_CALLBACK_URI", "http://localhost:8756/oauth-return")
    with pytest.raises(FinanceError, match="requires HTTPS"):
        LinkSettings.from_environment("production", credentials)
    assert LinkSettings.from_environment("sandbox", credentials).origin == "http://localhost:8756"


async def test_production_link_rejects_explicit_http_settings(app, fake):
    app.config = Config(environment="production")
    link = Link(app, settings=LinkSettings(callback_uri="http://localhost:8756/oauth-return"))
    with pytest.raises(FinanceError, match="requires HTTPS"):
        link.create_session()
    assert fake.calls == []


async def test_link_listener_address_is_independent_of_callback(app, tmp_path, monkeypatch):
    import socket

    from plaid_link.connect import running_server

    monkeypatch.setenv("OAUTH_CALLBACK_URI", "https://localhost:8756/oauth-return")
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    with running_server(tmp_path, env_file=None, host="0.0.0.0", port=port) as (
        runtime,
        _server,
        _thread,
    ):
        assert runtime.url.startswith("https://localhost:8756/#setup=")
        response = httpx.get(
            f"http://127.0.0.1:{port}", headers={"host": "localhost"}, trust_env=False
        )
        assert response.status_code == 200
