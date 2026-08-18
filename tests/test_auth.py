from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from moshi_data_pipeline.studio.auth import (
    AuthenticationService,
    AuthSettings,
    hash_password,
    token_hash,
    verify_password,
)
from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.server import create_studio_app

ORIGIN = "http://testserver"


class RecordingMailer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[str, str, str]] = []

    def send_activation(self, email: str, display_name: str, token: str) -> None:
        if self.fail:
            raise OSError("mail transport unavailable")
        self.messages.append((email, display_name, token))


def settings(*, require_sign_in: bool = True) -> AuthSettings:
    return AuthSettings(
        public_origin=ORIGIN,
        smtp_host="smtp.example.test",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_from="accounts@example.test",
        verification_ttl_seconds=3600,
        verification_resend_seconds=60,
        session_ttl_seconds=3600,
        cookie_name="moshi_session",
        cookie_secure=False,
        require_sign_in=require_sign_in,
    )


def test_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")
    assert first != second
    assert "correct horse battery staple" not in first
    assert verify_password("correct horse battery staple", first)
    assert not verify_password("wrong password", first)
    assert not verify_password("anything", "not-a-valid-hash")


def test_auth_settings_accept_existing_generic_smtp_names(monkeypatch) -> None:
    for name in (
        "MOSHI_PUBLIC_ORIGIN",
        "MOSHI_SMTP_HOST",
        "MOSHI_SMTP_PORT",
        "MOSHI_SMTP_USERNAME",
        "MOSHI_SMTP_PASSWORD",
        "MOSHI_SMTP_FROM",
        "MOSHI_SMTP_SECURITY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOSHI_TRUSTED_ORIGINS", "http://example.test")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_USERNAME", "mailer")
    monkeypatch.setenv("SMTP_PASSWORD", "test-secret")
    monkeypatch.setenv("SMTP_USE_TLS", "true")
    monkeypatch.setenv("EMAIL_FROM", "accounts@example.test")

    configured = AuthSettings.from_environment()

    assert configured.email_configured
    assert configured.public_origin == "http://example.test"
    assert configured.smtp_host == "smtp.example.test"
    assert configured.smtp_port == 2525
    assert configured.smtp_security == "starttls"


def test_signup_activation_signin_and_signout_are_durable(tmp_path) -> None:
    mailer = RecordingMailer()
    app = create_studio_app(
        tmp_path / "workspace",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        auth_settings=settings(),
        auth_mailer=mailer,
    )
    with TestClient(app) as client:
        assert client.get("/api/projects").status_code == 401
        signup = client.post(
            "/api/auth/signup",
            headers={"origin": ORIGIN},
            json={
                "display_name": "Alexandria Reviewer",
                "email": "Reviewer@Example.Test",
                "password": "secure-password",
            },
        )
        assert signup.status_code == 202
        assert len(mailer.messages) == 1
        activation_token = mailer.messages[0][2]

        user = app.state.studio.catalog.get_user_by_email("reviewer@example.test")
        assert user is not None
        assert user["status"] == "pending"
        assert user["password_hash"] != "secure-password"
        with app.state.studio.catalog.connect() as connection:
            stored_token = connection.execute(
                "SELECT token_hash FROM email_verification_tokens"
            ).fetchone()[0]
        assert stored_token == token_hash(activation_token)
        assert activation_token != stored_token

        pending = client.post(
            "/api/auth/signin",
            headers={"origin": ORIGIN},
            json={"email": "reviewer@example.test", "password": "secure-password"},
        )
        assert pending.status_code == 403

        activated = client.post(
            "/api/auth/activate",
            headers={"origin": ORIGIN},
            json={"token": activation_token},
        )
        assert activated.status_code == 200
        assert activated.json()["user"]["status"] == "active"

        signin = client.post(
            "/api/auth/signin",
            headers={"origin": ORIGIN},
            json={"email": "reviewer@example.test", "password": "secure-password"},
        )
        assert signin.status_code == 200
        cookie = signin.headers["set-cookie"].casefold()
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert client.get("/api/projects").status_code == 200
        assert client.get("/api/auth/me").json()["user"]["email"] == ("reviewer@example.test")

        with app.state.studio.catalog.connect() as connection:
            session = connection.execute("SELECT token_hash FROM user_sessions").fetchone()[0]
        assert len(session) == 64
        assert "moshi_session" not in session

        signed_out = client.post(
            "/api/auth/signout",
            headers={"origin": ORIGIN},
        )
        assert signed_out.status_code == 200
        assert client.get("/api/projects").status_code == 401
        assert client.get("/api/auth/me").json()["user"] is None


def test_authentication_rejects_untrusted_origin(tmp_path) -> None:
    app = create_studio_app(
        tmp_path / "workspace",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        auth_settings=settings(),
        auth_mailer=RecordingMailer(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/signup",
            headers={"origin": "https://attacker.invalid"},
            json={
                "display_name": "Blocked",
                "email": "blocked@example.test",
                "password": "secure-password",
            },
        )
    assert response.status_code == 403
    assert app.state.studio.catalog.get_user_by_email("blocked@example.test") is None


def test_failed_email_delivery_invalidates_token_for_immediate_retry(tmp_path) -> None:
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3")
    failing = RecordingMailer(fail=True)
    service = AuthenticationService(catalog, settings(), mailer=failing)
    try:
        service.signup(
            email="retry@example.test",
            password="secure-password",
            display_name="Retry User",
        )
    except OSError:
        pass
    else:
        raise AssertionError("Expected the simulated SMTP failure")

    with catalog.connect() as connection:
        active_tokens = connection.execute(
            """
            SELECT COUNT(*) FROM email_verification_tokens
            WHERE consumed_at IS NULL
            """
        ).fetchone()[0]
    assert active_tokens == 0

    recording = RecordingMailer()
    retried = AuthenticationService(catalog, settings(), mailer=recording)
    _, issued = retried.signup(
        email="retry@example.test",
        password="secure-password",
        display_name="Retry User",
    )
    assert issued is True
    assert len(recording.messages) == 1


def test_existing_v4_database_upgrades_with_auth_tables(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    catalog = StudioCatalog(path)
    with catalog.connect() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=5")
        connection.execute("DROP TABLE user_sessions")
        connection.execute("DROP TABLE email_verification_tokens")
        connection.execute("DROP TABLE users")

    StudioCatalog(path)
    with sqlite3.connect(path) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == 6
    assert {"users", "email_verification_tokens", "user_sessions"} <= tables
