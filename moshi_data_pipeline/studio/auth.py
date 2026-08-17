from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from moshi_data_pipeline.studio.catalog import StudioCatalog

_EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}$")
_PASSWORD_MIN_LENGTH = 8
_PASSWORD_MAX_LENGTH = 256
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LENGTH = 32


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str
    email: str
    password: str


class SigninRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str
    password: str


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    token: str


class ResendActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str


@dataclass(frozen=True)
class AuthSettings:
    public_origin: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_security: str = "starttls"
    verification_ttl_seconds: int = 24 * 60 * 60
    verification_resend_seconds: int = 60
    session_ttl_seconds: int = 7 * 24 * 60 * 60
    cookie_name: str = "moshi_session"
    cookie_secure: bool = False
    require_sign_in: bool = False

    @property
    def email_configured(self) -> bool:
        return bool(self.public_origin and self.smtp_host and self.smtp_from)

    @classmethod
    def from_environment(cls) -> AuthSettings:
        def environment_value(*names: str) -> str:
            for name in names:
                candidate = os.environ.get(name, "").strip()
                if candidate:
                    return candidate
            return ""

        public_origin = environment_value("MOSHI_PUBLIC_ORIGIN")
        if not public_origin:
            public_origin = environment_value("MOSHI_TRUSTED_ORIGINS").split(",", 1)[
                0
            ].strip()
        public_origin = public_origin.rstrip("/")
        smtp_host = environment_value("MOSHI_SMTP_HOST", "SMTP_HOST")
        smtp_from = environment_value("MOSHI_SMTP_FROM", "EMAIL_FROM")
        smtp_security = environment_value("MOSHI_SMTP_SECURITY")
        if not smtp_security:
            use_ssl = environment_value("SMTP_USE_SSL").casefold()
            use_tls = environment_value("SMTP_USE_TLS").casefold()
            if use_ssl in {"1", "true", "yes", "on"}:
                smtp_security = "ssl"
            elif use_tls in {"1", "true", "yes", "on"}:
                smtp_security = "starttls"
            else:
                smtp_security = "none"
        smtp_security = smtp_security.casefold()
        if smtp_security not in {"starttls", "ssl", "none"}:
            raise RuntimeError("MOSHI_SMTP_SECURITY must be starttls, ssl, or none")

        def positive(name: str, default: int, *aliases: str) -> int:
            raw = environment_value(name, *aliases)
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError as exc:
                raise RuntimeError(f"{name} must be an integer") from exc
            if value < 1:
                raise RuntimeError(f"{name} must be positive")
            return value

        secure_value = os.environ.get("MOSHI_SESSION_COOKIE_SECURE", "").strip()
        if secure_value not in {"", "0", "1"}:
            raise RuntimeError("MOSHI_SESSION_COOKIE_SECURE must be 0 or 1")
        require_sign_in_value = os.environ.get("MOSHI_REQUIRE_SIGN_IN", "0").strip()
        if require_sign_in_value not in {"0", "1"}:
            raise RuntimeError("MOSHI_REQUIRE_SIGN_IN must be 0 or 1")
        cookie_secure = (
            secure_value == "1" if secure_value else public_origin.casefold().startswith("https://")
        )
        cookie_name = (
            os.environ.get("MOSHI_SESSION_COOKIE_NAME", "moshi_session").strip() or "moshi_session"
        )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cookie_name):
            raise RuntimeError("MOSHI_SESSION_COOKIE_NAME is invalid")
        if public_origin and not re.fullmatch(r"https?://[^/\s]+", public_origin):
            raise RuntimeError("MOSHI_PUBLIC_ORIGIN must be an HTTP(S) origin")
        smtp_username = environment_value("MOSHI_SMTP_USERNAME", "SMTP_USERNAME")
        smtp_password = environment_value("MOSHI_SMTP_PASSWORD", "SMTP_PASSWORD")
        if bool(smtp_username) != bool(smtp_password):
            raise RuntimeError(
                "MOSHI_SMTP_USERNAME and MOSHI_SMTP_PASSWORD must be configured together"
            )
        return cls(
            public_origin=public_origin,
            smtp_host=smtp_host,
            smtp_port=positive("MOSHI_SMTP_PORT", 587, "SMTP_PORT"),
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            smtp_from=smtp_from,
            smtp_security=smtp_security,
            verification_ttl_seconds=positive("MOSHI_EMAIL_VERIFICATION_TTL_SECONDS", 24 * 60 * 60),
            verification_resend_seconds=positive("MOSHI_EMAIL_RESEND_COOLDOWN_SECONDS", 60),
            session_ttl_seconds=positive("MOSHI_AUTH_SESSION_TTL_SECONDS", 7 * 24 * 60 * 60),
            cookie_name=cookie_name,
            cookie_secure=cookie_secure,
            require_sign_in=require_sign_in_value == "1",
        )


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Enter a valid email address")
    return email


def validate_display_name(value: str) -> str:
    name = " ".join(value.strip().split())
    if not name or len(name) > 120 or any(ord(character) < 32 for character in name):
        raise ValueError("Display name must contain between 1 and 120 characters")
    return name


def validate_password(value: str) -> str:
    if len(value) < _PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {_PASSWORD_MIN_LENGTH} characters")
    if len(value) > _PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must be at most {_PASSWORD_MAX_LENGTH} characters")
    return value


def hash_password(password: str) -> str:
    value = validate_password(password)
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        value.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_LENGTH,
    )
    return "$".join(
        (
            "scrypt",
            "v1",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(derived).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, version, n, r, p, salt_value, expected_value = encoded.split("$")
        if scheme != "scrypt" or version != "v1":
            return False
        salt = base64.urlsafe_b64decode(salt_value.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_value.encode("ascii"))
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError, binascii.Error):
        return False
    return hmac.compare_digest(candidate, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_DUMMY_PASSWORD_HASH = hash_password("not-a-real-account-password")


def public_user(user: dict[str, object]) -> dict[str, object]:
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "role": user["role"],
        "status": user["status"],
        "group_name": user.get("group_name"),
        "email_verified_at": user.get("email_verified_at"),
    }


class ActivationMailer:
    def __init__(self, settings: AuthSettings):
        self.settings = settings

    def _connect(self) -> smtplib.SMTP:
        if not self.settings.email_configured:
            raise RuntimeError("Activation email delivery is not configured")
        if self.settings.smtp_security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=20,
            )
        else:
            client = smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=20,
            )
        if self.settings.smtp_security == "starttls":
            client.starttls()
        if self.settings.smtp_username:
            client.login(
                self.settings.smtp_username,
                self.settings.smtp_password,
            )
        return client

    @staticmethod
    def _close(client: smtplib.SMTP) -> None:
        try:
            client.quit()
        except smtplib.SMTPException:
            client.close()

    def check_connection(self) -> None:
        client = self._connect()
        try:
            client.noop()
        finally:
            self._close(client)

    def send_activation(self, email: str, display_name: str, token: str) -> None:
        link = f"{self.settings.public_origin}/#/activate?token={quote(token, safe='')}"
        message = EmailMessage()
        message["Subject"] = "Activate your Moshi account"
        message["From"] = self.settings.smtp_from
        message["To"] = email
        message.set_content(
            f"Hello {display_name},\n\n"
            "Activate your Moshi account using this link:\n"
            f"{link}\n\n"
            "If you did not request this account, ignore this email.\n"
        )
        client = self._connect()
        try:
            client.send_message(message)
        finally:
            self._close(client)


class AuthenticationService:
    def __init__(
        self,
        catalog: StudioCatalog,
        settings: AuthSettings,
        *,
        mailer: ActivationMailer | None = None,
    ):
        self.catalog = catalog
        self.settings = settings
        self.mailer = mailer or ActivationMailer(settings)

    def signup(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        group_name: str | None = None,
        send_email: bool = True,
    ) -> tuple[dict[str, object], bool]:
        normalized_email = normalize_email(email)
        clean_name = validate_display_name(display_name)
        password_hash = hash_password(password)
        user, created = self.catalog.create_or_refresh_pending_user(
            email=normalized_email,
            display_name=clean_name,
            password_hash=password_hash,
            group_name=group_name,
        )
        if str(user["status"]) != "pending":
            return public_user(user), False
        token = secrets.token_urlsafe(32)
        issued = self.catalog.issue_email_verification(
            str(user["id"]),
            token_hash(token),
            ttl_seconds=self.settings.verification_ttl_seconds,
            resend_cooldown_seconds=self.settings.verification_resend_seconds,
        )
        if issued and send_email:
            try:
                self.mailer.send_activation(normalized_email, clean_name, token)
            except Exception:
                self.catalog.invalidate_email_verification(token_hash(token))
                raise
        return public_user(user), bool(issued)

    def activate(self, token: str) -> dict[str, object]:
        if len(token) < 32 or len(token) > 256:
            raise ValueError("Activation token is invalid or expired")
        user = self.catalog.consume_email_verification(token_hash(token))
        if user is None:
            raise ValueError("Activation token is invalid or expired")
        return public_user(user)

    def resend_activation(self, email: str) -> bool:
        normalized_email = normalize_email(email)
        user = self.catalog.get_user_by_email(normalized_email)
        if user is None or user["status"] != "pending":
            return False
        token = secrets.token_urlsafe(32)
        issued = self.catalog.issue_email_verification(
            str(user["id"]),
            token_hash(token),
            ttl_seconds=self.settings.verification_ttl_seconds,
            resend_cooldown_seconds=self.settings.verification_resend_seconds,
        )
        if issued:
            try:
                self.mailer.send_activation(
                    str(user["email"]),
                    str(user["display_name"]),
                    token,
                )
            except Exception:
                self.catalog.invalidate_email_verification(token_hash(token))
                raise
        return issued

    def signin(self, email: str, password: str) -> tuple[dict[str, object], str]:
        normalized_email = normalize_email(email)
        user = self.catalog.get_user_by_email(normalized_email)
        valid = verify_password(
            password,
            str(user["password_hash"]) if user else _DUMMY_PASSWORD_HASH,
        )
        if not valid or user is None:
            raise ValueError("Email or password is incorrect")
        if user["status"] == "pending":
            raise PermissionError("Activate your account before signing in")
        if user["status"] != "active":
            raise PermissionError("This account is disabled")
        token = secrets.token_urlsafe(32)
        self.catalog.create_user_session(
            str(user["id"]),
            token_hash(token),
            ttl_seconds=self.settings.session_ttl_seconds,
        )
        return public_user(user), token

    def current_user(self, token: str | None) -> dict[str, object] | None:
        if not token:
            return None
        user = self.catalog.resolve_user_session(token_hash(token))
        return public_user(user) if user else None

    def signout(self, token: str | None) -> None:
        if token:
            self.catalog.revoke_user_session(token_hash(token))
