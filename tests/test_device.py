from __future__ import annotations

import json
import random
from datetime import UTC, datetime

import pytest
from azure.iot.device import Message, MethodRequest

from device import app as app_module
from device.app import INTERVAL_PROPERTY, DeviceApp
from device.machine import MIN_INTERVAL_MS, MachineSimulator, MachineState
from device.telemetry import SCHEMA_VERSION, build_payload, to_message


class FakeClient:
    def __init__(self, desired: dict | None = None) -> None:
        self.desired = desired or {}
        self.reported: list[dict] = []
        self.sent: list[Message] = []
        self.responses = []
        self.fail_send = False
        self.on_twin_desired_properties_patch_received = None
        self.on_method_request_received = None
        self.on_message_received = None

    async def get_twin(self) -> dict:
        return {"desired": self.desired, "reported": {}}

    async def patch_twin_reported_properties(self, patch: dict) -> None:
        self.reported.append(patch)

    async def send_message(self, message: Message) -> None:
        if self.fail_send:
            raise ConnectionError("link down")
        self.sent.append(message)

    async def send_method_response(self, response) -> None:
        self.responses.append(response)


@pytest.fixture
def machine() -> MachineSimulator:
    return MachineSimulator(MachineState(interval_ms=5000), rng=random.Random(42))


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def device(client: FakeClient, machine: MachineSimulator) -> DeviceApp:
    return DeviceApp(client, machine, "press-01")


def _c2d(body: bytes, message_id: str = "m-1") -> Message:
    message = Message(body)
    message.message_id = message_id
    return message


def test_machine_degrades_and_bands_health() -> None:
    state = MachineState()
    machine = MachineSimulator(state, rng=random.Random(1))
    assert machine.health() == "healthy"
    state.wear = 0.5
    assert machine.health() == "degraded"
    state.wear = 0.9
    assert machine.health() == "critical"
    machine.reset_wear()
    assert state.wear == 0.0


def test_machine_does_not_wear_while_stopped() -> None:
    state = MachineState(running=False)
    MachineSimulator(state, rng=random.Random(1)).step()
    assert state.wear == 0.0


def test_payload_contract() -> None:
    now = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
    payload = build_payload("press-01", 7, {"rpm": 1500.0}, "healthy", now)
    assert payload == {
        "schemaVersion": SCHEMA_VERSION,
        "deviceId": "press-01",
        "sequence": 7,
        "timestamp": "2026-01-02T03:04:05.678Z",
        "health": "healthy",
        "rpm": 1500.0,
    }


def test_message_carries_routing_properties() -> None:
    message = to_message(build_payload("press-01", 1, {"rpm": 1.0}, "degraded"))
    assert message.content_type == "application/json"
    assert message.content_encoding == "utf-8"
    assert message.custom_properties == {"schemaVersion": SCHEMA_VERSION, "health": "degraded"}
    assert message.message_id
    assert json.loads(message.data)["health"] == "degraded"


async def test_telemetry_sequence_increments(device: DeviceApp, client: FakeClient) -> None:
    await device.send_telemetry()
    await device.send_telemetry()
    sequences = [json.loads(m.data)["sequence"] for m in client.sent]
    assert sequences == [1, 2]


async def test_failed_send_is_logged_not_raised(device: DeviceApp, client: FakeClient) -> None:
    client.fail_send = True
    await device.send_telemetry()
    assert device.sequence == 1 and not client.sent


async def test_valid_desired_interval_is_applied_and_acknowledged(
    device: DeviceApp, client: FakeClient, machine: MachineSimulator
) -> None:
    await device.on_desired_patch({INTERVAL_PROPERTY: 2000, "$version": 4})
    assert machine.state.interval_ms == 2000
    assert client.reported[-1] == {INTERVAL_PROPERTY: {"value": 2000, "ac": 200, "av": 4, "ad": "applied"}}


@pytest.mark.parametrize("bad", [100, "2000", True, None, 2.5])
async def test_invalid_desired_interval_is_rejected(
    device: DeviceApp, client: FakeClient, machine: MachineSimulator, bad
) -> None:
    await device.on_desired_patch({INTERVAL_PROPERTY: bad, "$version": 5})
    ack = client.reported[-1][INTERVAL_PROPERTY]
    assert machine.state.interval_ms == 5000
    assert ack["ac"] == 400 and ack["av"] == 5 and ack["value"] == 5000


async def test_unrelated_desired_patch_is_ignored(device: DeviceApp, client: FakeClient) -> None:
    await device.on_desired_patch({"somethingElse": 1, "$version": 2})
    assert client.reported == []


async def test_set_sampling_rate_reports_device_initiated_change(
    device: DeviceApp, client: FakeClient, machine: MachineSimulator
) -> None:
    await device.on_desired_patch({INTERVAL_PROPERTY: 2000, "$version": 3})
    status, body = await device.dispatch_method("SetSamplingRate", {"intervalMs": 1000})
    assert (status, body) == (200, {"intervalMs": 1000})
    assert machine.state.interval_ms == 1000
    assert client.reported[-1][INTERVAL_PROPERTY] == {
        "value": 1000,
        "ac": 203,
        "av": 3,
        "ad": "set by SetSamplingRate",
    }


@pytest.mark.parametrize("payload", [None, {}, {"intervalMs": 10}, {"intervalMs": "1000"}, [1000]])
async def test_set_sampling_rate_validates(device: DeviceApp, payload) -> None:
    status, body = await device.dispatch_method("SetSamplingRate", payload)
    assert status == 400 and str(MIN_INTERVAL_MS) in body["error"]


async def test_diagnostics_and_unknown_method(device: DeviceApp) -> None:
    status, snapshot = await device.dispatch_method("RunDiagnostics", None)
    assert status == 200 and {"health", "wear", "running", "rpm"} <= snapshot.keys()
    status, body = await device.dispatch_method("SelfDestruct", None)
    assert status == 404 and "SelfDestruct" in body["error"]


async def test_reboot_resets_wear(device: DeviceApp, machine: MachineSimulator, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "REBOOT_DELAY_SECONDS", 0)
    machine.state.wear = 0.8
    status, _ = await device.dispatch_method("Reboot", None)
    assert status == 200 and machine.state.wear == 0.0 and machine.state.running


async def test_method_response_is_sent(device: DeviceApp, client: FakeClient) -> None:
    request = MethodRequest(request_id="1", name="RunDiagnostics", payload=None)
    await device.on_method(request)
    assert client.responses[0].status == 200 and client.responses[0].request_id == "1"


async def test_c2d_message_is_recorded_in_twin(device: DeviceApp, client: FakeClient) -> None:
    await device.on_c2d(_c2d(b'{"type":"maintenanceScheduled","window":"Sat 02:00"}', "abc"))
    record = client.reported[-1]["lastCloudMessage"]
    assert record["messageId"] == "abc" and record["type"] == "maintenanceScheduled"
    assert record["receivedAt"].endswith("Z")


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{"type": 5}', b'{"type": ""}', b"\xff"])
async def test_malformed_c2d_is_ignored(device: DeviceApp, client: FakeClient, body: bytes) -> None:
    await device.on_c2d(_c2d(body))
    assert client.reported == []


async def test_run_applies_twin_then_stops(client: FakeClient, machine: MachineSimulator) -> None:
    client.desired = {INTERVAL_PROPERTY: 1000, "$version": 1}
    device = DeviceApp(client, machine, "press-01")
    device.request_stop()
    await device.run()
    assert machine.state.interval_ms == 1000
    assert client.reported[1]["connectivity"] == "online"
    assert client.on_method_request_received == device.on_method


async def test_reported_failure_does_not_crash(device: DeviceApp, client: FakeClient) -> None:
    async def boom(patch):
        raise ConnectionError("down")

    client.patch_twin_reported_properties = boom
    await device.on_desired_patch({INTERVAL_PROPERTY: 1000, "$version": 1})
