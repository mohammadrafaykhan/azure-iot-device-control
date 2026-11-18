"""Environment-backed configuration for the iotctl service CLI."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_SECRET_NAME = "iothub-events-connection"
DEFAULT_CONSUMER_GROUP = "iotctl"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class ServiceConfig:
    hostname: str
    key_vault_uri: str | None
    telemetry_secret_name: str
    consumer_group: str

    def require_key_vault(self) -> str:
        if not self.key_vault_uri:
            raise ConfigError("KEY_VAULT_URI is required for telemetry monitoring")
        return self.key_vault_uri


def load_config() -> ServiceConfig:
    load_dotenv()
    hostname = os.getenv("IOTHUB_HOSTNAME", "").strip()
    if not hostname:
        raise ConfigError("Missing required environment variable: IOTHUB_HOSTNAME")
    return ServiceConfig(
        hostname=hostname,
        key_vault_uri=os.getenv("KEY_VAULT_URI", "").strip() or None,
        telemetry_secret_name=os.getenv("TELEMETRY_SECRET_NAME", DEFAULT_SECRET_NAME).strip(),
        consumer_group=os.getenv("CONSUMER_GROUP", DEFAULT_CONSUMER_GROUP).strip(),
    )
