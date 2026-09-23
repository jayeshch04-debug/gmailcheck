"""Settings from environment variables, an optional .env file and CLI overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SMTP_PRESETS = {
    "gmail": {"host": "smtp.gmail.com", "port": 587, "security": "starttls"},
    "outlook": {"host": "smtp.office365.com", "port": 587, "security": "starttls"},
}


class ConfigError(Exception):
    pass


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, # comments, optional quotes.

    Real environment variables take precedence over the file.
    """
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if value[:1] in ("\"", "'") and value[0] in value[1:]:
            value = value[1:value.index(value[0], 1)]
        else:
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    security: str  # "starttls" | "ssl" | "none"

    def validate(self) -> None:
        missing = [n for n, v in (("SMTP_HOST", self.host), ("SMTP_USER", self.user),
                                  ("SMTP_PASS", self.password)) if not v]
        if missing:
            raise ConfigError(f"missing SMTP settings: {', '.join(missing)}")
        if self.security not in ("starttls", "ssl", "none"):
            raise ConfigError("SMTP_SECURITY must be starttls, ssl or none")


@dataclass
class ImapConfig:
    host: str
    port: int
    user: str
    password: str

    def validate(self, prefix: str) -> None:
        missing = [f"{prefix}_{n}" for n, v in (("USER", self.user), ("PASS", self.password)) if not v]
        if missing:
            raise ConfigError(f"missing IMAP settings: {', '.join(missing)}")


def smtp_config(preset: str | None = None) -> SmtpConfig:
    preset = preset or _env("SMTP_PRESET") or None
    base = {"host": "", "port": 587, "security": "starttls"}
    if preset:
        if preset not in SMTP_PRESETS:
            raise ConfigError(f"unknown SMTP preset {preset!r} (choose: {', '.join(SMTP_PRESETS)})")
        base = dict(SMTP_PRESETS[preset])
    user = _env("SMTP_USER")
    security = _env("SMTP_SECURITY", base["security"]).lower()
    default_port = 465 if security == "ssl" else base["port"]
    return SmtpConfig(
        host=_env("SMTP_HOST", base["host"]),
        port=int(_env("SMTP_PORT", str(default_port))),
        user=user,
        password=_env("SMTP_PASS").replace(" ", "") if preset == "gmail" else _env("SMTP_PASS"),
        sender=_env("SMTP_FROM", user),
        security=security,
    )


def _imap(prefix: str) -> ImapConfig:
    return ImapConfig(
        host=_env(f"{prefix}_HOST", "imap.gmail.com"),
        port=int(_env(f"{prefix}_PORT", "993")),
        user=_env(f"{prefix}_USER"),
        # Gmail shows app passwords in groups of four with spaces; strip them.
        password=_env(f"{prefix}_PASS").replace(" ", ""),
    )


def master_imap_config() -> ImapConfig:
    return _imap("IMAP")


def bounce_imap_config() -> ImapConfig | None:
    """IMAP access to the sender mailbox for bounce detection; None if not configured."""
    cfg = _imap("BOUNCE_IMAP")
    return cfg if cfg.user and cfg.password else None
