"""Application configuration comes from the project's .env, not ambient variables."""
from pathlib import Path
from urllib.parse import urlsplit
import ipaddress

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    host: str = "127.0.0.1"
    port: int = Field(default=8001, ge=1, le=65535)
    data_path: Path = ROOT / "data"
    name: str = Field(default="Website Assistant", min_length=1, max_length=100)
    home_url: str = ""
    admin_username: str = ""
    admin_password_hash: SecretStr = SecretStr("")
    admin_role: str = "administrator"
    admin_session_minutes: int = Field(default=30, ge=5, le=120)
    admin_cookie_secure: bool = True
    ai_api_endpoint: str = ""
    ai_api_key: SecretStr = SecretStr("")
    ai_model: str = Field(default="", max_length=200)
    ai_timeout_seconds: float = Field(default=30, ge=1, le=120, allow_inf_nan=False)
    chat_rate_limit: int = Field(default=60, ge=1, le=1000)
    auto_activate: bool = True
    max_source_drop_fraction: float = Field(default=.3, ge=0, le=1, allow_inf_nan=False)

    @field_validator("ai_api_endpoint")
    @classmethod
    def endpoint(cls, value):
        if not value:
            return value
        parsed = urlsplit(value)
        if (not parsed.hostname or parsed.scheme not in {"http", "https"}
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port == 0
                or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value):
            raise ValueError("Use a complete provider HTTP(S) endpoint without credentials, query, or fragment")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if parsed.scheme == "http" and not loopback:
            raise ValueError("Provider requires HTTPS except for loopback development")
        return value

    @classmethod
    def load(cls, path: Path | None = None):
        path = path or ROOT / ".env"
        prefix = "WEBSITE_ASSISTANT_"
        values = {key.removeprefix(prefix).lower(): value
                  for key, value in dotenv_values(path, interpolate=False).items()
                  if key.startswith(prefix) and value is not None}
        data = Path(values.get("data_path", "data"))
        values["data_path"] = (path.parent / data).resolve() if not data.is_absolute() else data.resolve()
        return cls.model_validate(values)
