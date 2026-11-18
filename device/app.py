"""Device application: telemetry loop plus twin, direct-method, and C2D handlers.

Writable twin properties are acknowledged with the IoT Plug and Play convention
(``value``, ``ac`` status code, ``av`` acknowledged version, ``ad`` description)
so the cloud can tell whether a desired setting was applied or rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from azure.iot.device import Message, MethodRequest, MethodResponse

from device.machine import MIN_INTERVAL_MS, MachineSimulator
from device.telemetry import build_payload, to_message

logger = logging.getLogger("devctl.device")

FIRMWARE_VERSION = "1.0.0"
REBOOT_DELAY_SECONDS = 1.0
INTERVAL_PROPERTY = "telemetryIntervalMs"


class DeviceClient(Protocol):
    on_twin_desired_properties_patch_received: Any
    on_method_request_received: Any
    on_message_received: Any

    async def get_twin(self) -> dict: ...
    async def patch_twin_reported_properties(self, patch: dict) -> None: ...
    async def send_message(self, message: Message) -> None: ...
    async def send_method_response(self, response: MethodResponse) -> None: ...


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class DeviceApp:
    """Owns the connected client, the simulated machine, and all cloud handlers."""

    def __init__(self, client: DeviceClient, machine: MachineSimulator, device_id: str) -> None:
        self._client = client
        self._machine = machine
        self._device_id = device_id
        self._sequence = 0
        self._desired_version: int | None = None
        self._stop = asyncio.Event()

    @property
    def sequence(self) -> int:
        return self._sequence

    def request_stop(self) -> None:
        self._stop.set()

    def attach_handlers(self) -> None:
        self._client.on_twin_desired_properties_patch_received = self.on_desired_patch
        self._client.on_method_request_received = self.on_method
        self._client.on_message_received = self.on_c2d

    async def run(self) -> None:
        self.attach_handlers()
        twin = await self._client.get_twin()
        await self.on_desired_patch(twin.get("desired", {}))
        await self._report(
            {
                "firmwareVersion": FIRMWARE_VERSION,
                "machineHealth": self._machine.health(),
                "lastReboot": int(self._machine.state.last_reboot),
                "connectivity": "online",
            }
        )
        while not self._stop.is_set():
            if self._machine.state.running:
                await self.send_telemetry()
            await self._sleep(self._machine.state.interval_ms / 1000)

    async def shutdown(self) -> None:
        await self._report({"connectivity": "offline"})

    async def send_telemetry(self) -> None:
        self._machine.step()
        self._sequence += 1
        payload = build_payload(self._device_id, self._sequence, self._machine.read(), self._machine.health())
        try:
            await self._client.send_message(to_message(payload))
            logger.info("telemetry sent seq=%d health=%s", payload["sequence"], payload["health"])
        except Exception:
            logger.exception("telemetry send failed seq=%d", payload["sequence"])

    async def on_desired_patch(self, patch: dict) -> None:
        if INTERVAL_PROPERTY not in patch:
            return
        version = patch.get("$version")
        self._desired_version = version
        requested = patch[INTERVAL_PROPERTY]
        if isinstance(requested, int) and not isinstance(requested, bool) and requested >= MIN_INTERVAL_MS:
            self._machine.state.interval_ms = requested
            ack = {"value": requested, "ac": 200, "av": version, "ad": "applied"}
            logger.info("telemetry interval set to %d ms (desired v%s)", requested, version)
        else:
            ack = {
                "value": self._machine.state.interval_ms,
                "ac": 400,
                "av": version,
                "ad": f"must be an integer >= {MIN_INTERVAL_MS}",
            }
            logger.warning("rejected desired %s=%r (v%s)", INTERVAL_PROPERTY, requested, version)
        await self._report({INTERVAL_PROPERTY: ack})

    async def on_method(self, request: MethodRequest) -> None:
        logger.info("direct method %s", request.name)
        status, payload = await self.dispatch_method(request.name, request.payload)
        await self._client.send_method_response(
            MethodResponse.create_from_method_request(request, status, payload)
        )

    async def dispatch_method(self, name: str, payload: Any) -> tuple[int, dict]:
        if name == "SetSamplingRate":
            return await self._set_sampling_rate(payload)
        if name == "RunDiagnostics":
            return 200, self._machine.snapshot()
        if name == "Reboot":
            return await self._reboot()
        return 404, {"error": f"unknown method: {name}"}

    async def on_c2d(self, message: Message) -> None:
        try:
            body = json.loads(message.data)
            kind = body["type"]
            if not isinstance(kind, str) or not kind:
                raise ValueError("type must be a non-empty string")
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("ignored malformed C2D message %s: %s", message.message_id, exc)
            return
        logger.info("C2D %s received: %s", message.message_id, kind)
        await self._report(
            {"lastCloudMessage": {"messageId": message.message_id, "type": kind, "receivedAt": _utc_now()}}
        )

    async def _set_sampling_rate(self, payload: Any) -> tuple[int, dict]:
        interval = payload.get("intervalMs") if isinstance(payload, dict) else None
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < MIN_INTERVAL_MS:
            return 400, {"error": f"intervalMs must be an integer >= {MIN_INTERVAL_MS}"}
        self._machine.state.interval_ms = interval
        await self._report(
            {
                INTERVAL_PROPERTY: {
                    "value": interval,
                    "ac": 203,
                    "av": self._desired_version,
                    "ad": "set by SetSamplingRate",
                }
            }
        )
        return 200, {"intervalMs": interval}

    async def _reboot(self) -> tuple[int, dict]:
        state = self._machine.state
        state.running = False
        await self._sleep(REBOOT_DELAY_SECONDS)
        self._machine.reset_wear()
        state.last_reboot = time.time()
        state.running = True
        await self._report({"lastReboot": int(state.last_reboot), "machineHealth": self._machine.health()})
        return 200, {"result": "rebooted"}

    async def _report(self, patch: dict) -> None:
        try:
            await self._client.patch_twin_reported_properties(patch)
        except Exception:
            logger.exception("reported-property update failed: %s", sorted(patch))

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
