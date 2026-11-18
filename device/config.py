"""Environment-backed configuration for the device simulator."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class DeviceConfig:
    hostname: str
    device_id: str
    cert_file: str
    key_file: str
    key_passphrase: str | None


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _require_file(name: str) -> str:
    path = _require(name)
    if not os.path.isfile(path):
        raise ConfigError(f"{name} points to a missing file: {path}")
    return path


def load_config() -> DeviceConfig:
    load_dotenv()
    return DeviceConfig(
        hostname=_require("IOTHUB_HOSTNAME"),
        device_id=_require("DEVICE_ID"),
        cert_file=_require_file("X509_CERT_FILE"),
        key_file=_require_file("X509_KEY_FILE"),
        key_passphrase=os.getenv("X509_KEY_PASSPHRASE") or None,
    )
