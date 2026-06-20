from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required server configuration is missing or invalid."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8443
    base_url: str = "https://localhost:8443"
    import_token: str = ""
    storage_dir: Path = Path("./data/imports")
    retention_hours: int = 24
    max_upload_bytes: int = 10 * 1024 * 1024
    tls_cert: Path | None = None
    tls_key: Path | None = None
    dev_http: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        dev_http = _env_bool("RENDERER_DEV_HTTP", False)
        host = os.getenv("RENDERER_HOST", "0.0.0.0")
        port = _env_int("RENDERER_PORT", 8443)
        base_url = os.getenv(
            "RENDERER_BASE_URL",
            f"http://localhost:{port}" if dev_http else f"https://localhost:{port}",
        ).rstrip("/")
        import_token = os.getenv("RENDERER_IMPORT_TOKEN", "")
        storage_dir = Path(os.getenv("RENDERER_STORAGE_DIR", "./data/imports"))
        retention_hours = _env_int("RENDERER_RETENTION_HOURS", 24)
        max_upload_bytes = _env_int("RENDERER_MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
        tls_cert_raw = os.getenv("RENDERER_TLS_CERT")
        tls_key_raw = os.getenv("RENDERER_TLS_KEY")

        settings = cls(
            host=host,
            port=port,
            base_url=base_url,
            import_token=import_token,
            storage_dir=storage_dir,
            retention_hours=retention_hours,
            max_upload_bytes=max_upload_bytes,
            tls_cert=Path(tls_cert_raw) if tls_cert_raw else None,
            tls_key=Path(tls_key_raw) if tls_key_raw else None,
            dev_http=dev_http,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.import_token and not self.dev_http:
            raise ConfigError(
                "RENDERER_IMPORT_TOKEN is required unless RENDERER_DEV_HTTP=1 is set"
            )
        if self.retention_hours < 0:
            raise ConfigError("RENDERER_RETENTION_HOURS must be non-negative")
        if self.max_upload_bytes <= 0:
            raise ConfigError("RENDERER_MAX_UPLOAD_BYTES must be positive")
        if not self.dev_http and (not self.tls_cert or not self.tls_key):
            raise ConfigError(
                "RENDERER_TLS_CERT and RENDERER_TLS_KEY are required unless "
                "RENDERER_DEV_HTTP=1 is set"
            )

    @property
    def effective_import_token(self) -> str:
        return self.import_token or "dev-token"
