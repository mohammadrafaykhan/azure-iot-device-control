"""Live device-to-cloud telemetry from IoT Hub's built-in endpoint.

The built-in Event Hubs-compatible endpoint only accepts a shared-access key, so
the connection string lives in Key Vault and is fetched at runtime with the
operator's Entra ID identity (see docs/adr/0003). It is never written to disk.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from azure.core.credentials import TokenCredential
from azure.eventhub import EventData, EventHubConsumerClient, PartitionContext
from azure.keyvault.secrets import SecretClient

logger = logging.getLogger("devctl.monitor")

DEVICE_ID_PROPERTY = b"iothub-connection-device-id"


def fetch_connection_string(vault_uri: str, secret_name: str, credential: TokenCredential) -> str:
    with SecretClient(vault_url=vault_uri, credential=credential) as client:
        value = client.get_secret(secret_name).value
    if not value:
        raise RuntimeError(f"Key Vault secret '{secret_name}' is empty")
    return value


def _text(value: Any) -> Any:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def format_event(
    system_properties: Mapping[bytes, Any],
    application_properties: Mapping[bytes | str, Any] | None,
    body: bytes,
    enqueued_time: datetime | None,
    device_filter: str | None = None,
) -> dict | None:
    """Shape one Event Hubs event into a printable record, or None if filtered out."""
    device_id = _text(system_properties.get(DEVICE_ID_PROPERTY))
    if device_filter and device_id != device_filter:
        return None
    try:
        payload: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        payload = _text(body)
    return {
        "deviceId": device_id,
        "enqueuedTime": enqueued_time.isoformat() if enqueued_time else None,
        "properties": {_text(k): _text(v) for k, v in (application_properties or {}).items()},
        "body": payload,
    }


class TelemetryMonitor:
    """Streams telemetry until interrupted; the consumer client is always closed."""

    def __init__(
        self,
        connection_string: str,
        consumer_group: str,
        device_filter: str | None = None,
        sink: Callable[[dict], None] | None = None,
    ) -> None:
        self._connection_string = connection_string
        self._consumer_group = consumer_group
        self._device_filter = device_filter
        self._sink = sink or (lambda record: print(json.dumps(record), flush=True))

    def _on_event(self, partition_context: PartitionContext, event: EventData | None) -> None:
        if event is None:
            return
        record = format_event(
            event.system_properties,
            event.properties,
            b"".join(event.body) if not isinstance(event.body, bytes) else event.body,
            event.enqueued_time,
            self._device_filter,
        )
        if record is not None:
            self._sink(record)

    def _on_error(self, partition_context: PartitionContext | None, error: Exception) -> None:
        partition = partition_context.partition_id if partition_context else "-"
        logger.warning("receive error on partition %s: %s", partition, error)

    def run(self) -> None:
        client = EventHubConsumerClient.from_connection_string(
            self._connection_string, consumer_group=self._consumer_group
        )
        logger.info("monitoring telemetry (consumer group %s); Ctrl+C to stop", self._consumer_group)
        with client:
            client.receive(on_event=self._on_event, on_error=self._on_error, starting_position="@latest")
