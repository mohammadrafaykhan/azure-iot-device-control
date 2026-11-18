"""IoT Hub service operations behind one adapter.

This is the only module that imports ``azure-iot-hub``. That SDK depends on the
deprecated ``uamqp`` library (see docs/adr/0002), so isolating it here keeps a
future migration to a single file. Authentication is Microsoft Entra ID only:
no shared-access keys are used for registry, twin, method, or C2D operations.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential
from azure.iot.hub import IoTHubRegistryManager
from azure.iot.hub.models import CloudToDeviceMethod, Twin, TwinProperties
from msrest.exceptions import HttpOperationError

MIN_METHOD_TIMEOUT_SECONDS = 5
MAX_METHOD_TIMEOUT_SECONDS = 300
MAX_C2D_BYTES = 64 * 1024


class HubError(RuntimeError):
    """A service operation failed; the message is safe to show to an operator."""


@dataclass(frozen=True)
class MethodResult:
    status: int
    payload: Any


def _describe(error: HttpOperationError) -> str:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", "?")
    return f"IoT Hub returned HTTP {status}: {error.message}"


class HubService:
    """Registry, twin, direct-method, and C2D operations for one IoT hub."""

    def __init__(self, registry: IoTHubRegistryManager) -> None:
        self._registry = registry

    @classmethod
    def connect(cls, hostname: str, credential: TokenCredential) -> HubService:
        return cls(IoTHubRegistryManager.from_token_credential(hostname, credential))

    def __enter__(self) -> HubService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        amqp = getattr(self._registry, "amqp_svc_client", None)
        if amqp is not None:
            amqp.disconnect_sync()
            self._registry.amqp_svc_client = None

    def list_devices(self, limit: int = 100) -> list[dict]:
        try:
            devices = self._registry.get_devices(max_number_of_devices=limit)
        except HttpOperationError as exc:
            raise HubError(_describe(exc)) from exc
        return [
            {
                "deviceId": d.device_id,
                "status": d.status,
                "connectionState": d.connection_state,
                "authentication": d.authentication.type if d.authentication else None,
                "lastActivityTime": d.last_activity_time.isoformat() if d.last_activity_time else None,
            }
            for d in devices
        ]

    def register_x509_device(
        self, device_id: str, primary_thumbprint: str, secondary_thumbprint: str
    ) -> dict:
        try:
            device = self._registry.create_device_with_x509(
                device_id, primary_thumbprint, secondary_thumbprint, "enabled"
            )
        except HttpOperationError as exc:
            raise HubError(_describe(exc)) from exc
        return {"deviceId": device.device_id, "status": device.status, "authentication": "selfSigned"}

    def delete_device(self, device_id: str) -> None:
        try:
            self._registry.delete_device(device_id)
        except HttpOperationError as exc:
            raise HubError(_describe(exc)) from exc

    def get_twin(self, device_id: str) -> dict:
        try:
            twin = self._registry.get_twin(device_id)
        except HttpOperationError as exc:
            raise HubError(_describe(exc)) from exc
        return {
            "deviceId": twin.device_id,
            "etag": twin.etag,
            "tags": twin.tags or {},
            "desired": twin.properties.desired if twin.properties else {},
            "reported": twin.properties.reported if twin.properties else {},
        }

    def set_desired(self, device_id: str, patch: dict) -> dict:
        """Merge-patch desired properties, guarded by the twin's ETag (optimistic concurrency)."""
        if not patch:
            raise HubError("desired patch is empty")
        try:
            current = self._registry.get_twin(device_id)
            updated = self._registry.update_twin(
                device_id, Twin(properties=TwinProperties(desired=patch)), current.etag
            )
        except HttpOperationError as exc:
            raise HubError(_describe(exc)) from exc
        return updated.properties.desired if updated.properties else {}

    def invoke_method(
        self, device_id: str, name: str, payload: Any, timeout_seconds: int = 30
    ) -> MethodResult:
        if not MIN_METHOD_TIMEOUT_SECONDS <= timeout_seconds <= MAX_METHOD_TIMEOUT_SECONDS:
            raise HubError(
                f"timeout must be {MIN_METHOD_TIMEOUT_SECONDS}-{MAX_METHOD_TIMEOUT_SECONDS} seconds"
            )
        request = CloudToDeviceMethod(
            method_name=name, payload=payload, response_timeout_in_seconds=timeout_seconds
        )
        try:
            result = self._registry.invoke_device_method(device_id, request)
        except HttpOperationError as exc:
            raise HubError(_describe(exc)) from exc
        return MethodResult(status=result.status, payload=result.payload)

    def send_c2d(self, device_id: str, body: dict, ttl_seconds: int | None = None) -> None:
        data = json.dumps(body, separators=(",", ":"))
        if len(data.encode("utf-8")) > MAX_C2D_BYTES:
            raise HubError(f"C2D message exceeds {MAX_C2D_BYTES} bytes")
        properties: dict[str, Any] = {"contentType": "application/json", "contentEncoding": "utf-8"}
        if ttl_seconds is not None:
            properties["expiryTimeUtc"] = _expiry_ms(ttl_seconds)
        try:
            self._registry.send_c2d_message(device_id, data, properties=properties)
        except Exception as exc:
            raise HubError(f"C2D send failed: {exc}") from exc


def _expiry_ms(ttl_seconds: int) -> int:
    if ttl_seconds <= 0:
        raise HubError("ttl must be positive")
    return int((time.time() + ttl_seconds) * 1000)
