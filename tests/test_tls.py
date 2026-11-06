import json
import selectors
import signal
import socket
import ssl
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from plaid_core.config import Config, LinkSettings
from plaid_core.errors import FinanceError
from plaid_link import connect as link
from pydantic import ValidationError


@pytest.fixture
def certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    stamp = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(stamp - timedelta(minutes=1))
        .not_valid_after(stamp + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = tmp_path / "certificate.pem", tmp_path / "private.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    keyfile.chmod(0o600)
    return certfile, keyfile


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def start_cli(root, *args):
    return subprocess.Popen(
        [str(Path(sys.executable).with_name("plaid-agent")), "--data-dir", str(root), *args],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def ready_url(process, stream):
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        for _ in range(20):
            if not selector.select(timeout=0.5):
                assert process.poll() is None, "Installed TLS command exited before readiness"
                continue
            line = stream.readline()
            if "in your browser:" in line:
                url = stream.readline().strip()
                assert url.startswith("https://localhost:")
                return url
    raise AssertionError("Installed command did not emit a ready HTTPS URL")


def stop_cli(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        process.communicate(timeout=12)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


def finish(url, certfile):
    from urllib.parse import parse_qs, urlsplit

    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    trust = ssl.create_default_context(cafile=str(certfile))
    with httpx.Client(
        base_url=origin, verify=trust, headers={"origin": origin}, trust_env=False
    ) as browser:
        assert browser.get("/").status_code == 200
        token = parse_qs(parsed.fragment)["setup"][0]
        assert browser.post("/api/session", json={"token": token}).status_code == 204
        assert browser.post("/api/setup/finish", json={}).status_code == 204


def test_installed_https_connect_uses_configured_certificate_and_finishes(
    app, tmp_path, certificate, monkeypatch
):
    certfile, keyfile = certificate
    port = free_port()
    monkeypatch.setenv("OAUTH_CALLBACK_URI", f"https://localhost:{port}/oauth-return")
    process = start_cli(
        tmp_path,
        "--json",
        "config",
        "set",
        "--environment",
        "production",
        "--link-port",
        str(port),
        "--link-certfile",
        str(certfile),
        "--link-keyfile",
        str(keyfile),
    )
    output, _ = process.communicate(timeout=10)
    assert process.returncode == 0
    assert json.loads(output)["link_certfile"] == str(certfile)
    process = start_cli(tmp_path, "connect", "--no-open")
    try:
        url = ready_url(process, process.stderr)
        runtime = json.loads((tmp_path / "link-server.json").read_text())
        assert runtime["scheme"] == "https"
        finish(url, certfile)
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        stop_cli(process)
    assert not (tmp_path / "link-server.json").exists()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))


def test_installed_connect_reuses_https_serve_without_stopping_it(
    app, tmp_path, certificate, monkeypatch
):
    certfile, keyfile = certificate
    port = free_port()
    monkeypatch.setenv("OAUTH_CALLBACK_URI", f"https://localhost:{port}/oauth-return")
    flags = ["--port", str(port), "--certfile", str(certfile), "--keyfile", str(keyfile)]
    server = start_cli(tmp_path, "link", "serve", *flags)
    connecting = None
    try:
        url = ready_url(server, server.stdout)
        connecting = start_cli(tmp_path, "connect", "--no-open", *flags)
        reused = ready_url(connecting, connecting.stderr)
        assert reused == url
        finish(reused, certfile)
        connecting.wait(timeout=10)
        assert connecting.returncode == 0
        assert server.poll() is None
    finally:
        if connecting is not None:
            stop_cli(connecting)
        stop_cli(server)
    assert server.returncode == 0
    assert not (tmp_path / "link-server.json").exists()


def test_invalid_tls_and_hostname_do_not_replace_a_running_listener(
    app, tmp_path, certificate, monkeypatch
):
    certfile, keyfile = certificate
    with pytest.raises(ValidationError, match="both link_certfile and link_keyfile"):
        Config(link_certfile=str(certfile))
    with pytest.raises(FinanceError, match="both --certfile and --keyfile"):
        link.connect(tmp_path, certfile=str(certfile), open_browser=False)
    with pytest.raises(FinanceError, match="Cannot load TLS"):
        with link.running_server(
            tmp_path,
            env_file=None,
            host="127.0.0.1",
            port=free_port(),
            certfile=str(certfile),
            keyfile=str(certfile),
        ):
            raise AssertionError("An invalid key must not start a listener")
    port = free_port()
    monkeypatch.setenv("OAUTH_CALLBACK_URI", f"https://wrong.example.test:{port}/oauth-return")
    with link.running_server(
        tmp_path,
        env_file=None,
        host="127.0.0.1",
        port=port,
        certfile=str(certfile),
        keyfile=str(keyfile),
    ) as (_, _, thread):
        before = (tmp_path / "link-server.json").read_bytes()
        settings = LinkSettings.from_environment("sandbox")
        with pytest.raises(FinanceError, match="Cannot verify.*TLS"):
            link._existing(tmp_path, app.config, settings, str(certfile))
        assert thread.is_alive()
        assert (tmp_path / "link-server.json").read_bytes() == before
