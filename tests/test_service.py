from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from msrest.exceptions import HttpOperationError

from service import cli
from service.config import ConfigError, ServiceConfig
from service.hub import HubError, HubService, MethodResult
from service.monitor import format_event
from tools.make_device_cert import create_certificate, write_files


def _http_error(status: int, message: str) -> HttpOperationError:
    error = HttpOperationError.__new__(HttpOperationError)
    error.message = message
    error.response = SimpleNamespace(status_code=status)
    return error


class FakeAmqp:
    def __init__(self) -> None:
        self.closed = False

    def disconnect_sync(self) -> None:
        self.closed = True


class FakeRegistry:
    def __init__(self) -> None:
        self.amqp_svc_client = FakeAmqp()
        self.calls: list[tuple] = []
        self.twin = SimpleNamespace(
            device_id="press-01",
            etag="etag-1",
            tags=None,
            properties=SimpleNamespace(
                desired={"telemetryIntervalMs": 5000}, reported={"connectivity": "online"}
            ),
        )
        self.fail_with: Exception | None = None

    def _maybe_fail(self) -> None:
        if self.fail_with:
            raise self.fail_with

    def get_devices(self, max_number_of_devices=None):
        self._maybe_fail()
        return [
            SimpleNamespace(
                device_id="press-01",
                status="enabled",
                connection_state="Connected",
                authentication=SimpleNamespace(type="selfSigned"),
                last_activity_time=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ]

    def create_device_with_x509(self, device_id, primary, secondary, status):
        self.calls.append(("create", device_id, primary, secondary, status))
        return SimpleNamespace(device_id=device_id, status=status)

    def delete_device(self, device_id):
        self._maybe_fail()
        self.calls.append(("delete", device_id))

    def get_twin(self, device_id):
        self._maybe_fail()
        return self.twin

    def update_twin(self, device_id, twin, etag):
        self.calls.append(("update", device_id, twin.properties.desired, etag))
        return SimpleNamespace(
            properties=SimpleNamespace(desired={**self.twin.properties.desired, **twin.properties.desired})
        )

    def invoke_device_method(self, device_id, request):
        self.calls.append(
            ("invoke", device_id, request.method_name, request.payload, request.response_timeout_in_seconds)
        )
        return SimpleNamespace(status=200, payload={"ok": True})

    def send_c2d_message(self, device_id, data, properties):
        self._maybe_fail()
        self.calls.append(("c2d", device_id, data, properties))


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def hub(registry: FakeRegistry) -> HubService:
    return HubService(registry)


def test_close_disconnects_amqp_once(hub: HubService, registry: FakeRegistry) -> None:
    amqp = registry.amqp_svc_client
    with hub:
        pass
    assert amqp.closed and registry.amqp_svc_client is None
    hub.close()


def test_list_devices_shapes_output(hub: HubService) -> None:
    [device] = hub.list_devices()
    assert device == {
        "deviceId": "press-01",
        "status": "enabled",
        "connectionState": "Connected",
        "authentication": "selfSigned",
        "lastActivityTime": "2026-01-01T00:00:00+00:00",
    }


def test_set_desired_uses_etag(hub: HubService, registry: FakeRegistry) -> None:
    desired = hub.set_desired("press-01", {"telemetryIntervalMs": 2000})
    assert registry.calls[-1] == ("update", "press-01", {"telemetryIntervalMs": 2000}, "etag-1")
    assert desired["telemetryIntervalMs"] == 2000


def test_set_desired_rejects_empty_patch(hub: HubService) -> None:
    with pytest.raises(HubError):
        hub.set_desired("press-01", {})


def test_invoke_method(hub: HubService, registry: FakeRegistry) -> None:
    result = hub.invoke_method("press-01", "SetSamplingRate", {"intervalMs": 1000}, 15)
    assert result == MethodResult(200, {"ok": True})
    assert registry.calls[-1] == ("invoke", "press-01", "SetSamplingRate", {"intervalMs": 1000}, 15)


@pytest.mark.parametrize("timeout", [0, 4, 301])
def test_invoke_rejects_out_of_range_timeout(hub: HubService, timeout: int) -> None:
    with pytest.raises(HubError):
        hub.invoke_method("press-01", "RunDiagnostics", None, timeout)


def test_send_c2d_sets_json_properties_and_expiry(hub: HubService, registry: FakeRegistry) -> None:
    hub.send_c2d("press-01", {"type": "ping"}, ttl_seconds=60)
    _, device_id, data, properties = registry.calls[-1]
    assert device_id == "press-01" and json.loads(data) == {"type": "ping"}
    assert properties["contentType"] == "application/json"
    assert properties["expiryTimeUtc"] > 0


def test_send_c2d_enforces_size_limit(hub: HubService) -> None:
    with pytest.raises(HubError):
        hub.send_c2d("press-01", {"type": "x", "blob": "a" * 70_000})


def test_http_errors_are_mapped(hub: HubService, registry: FakeRegistry) -> None:
    registry.fail_with = _http_error(404, "DeviceNotFound")
    with pytest.raises(HubError, match="HTTP 404"):
        hub.get_twin("ghost")


def test_c2d_transport_errors_are_mapped(hub: HubService, registry: FakeRegistry) -> None:
    registry.fail_with = Exception("C2D message send failure")
    with pytest.raises(HubError, match="C2D send failed"):
        hub.send_c2d("press-01", {"type": "ping"})


def test_format_event_decodes_and_filters() -> None:
    system = {b"iothub-connection-device-id": b"press-01"}
    record = format_event(system, {b"health": b"critical"}, b'{"rpm": 1}', datetime(2026, 1, 1, tzinfo=UTC))
    assert record == {
        "deviceId": "press-01",
        "enqueuedTime": "2026-01-01T00:00:00+00:00",
        "properties": {"health": "critical"},
        "body": {"rpm": 1},
    }
    assert format_event(system, None, b"{}", None, device_filter="press-02") is None
    assert format_event(system, None, b"plain text", None)["body"] == "plain text"


# ---------------------------------------------------------------- CLI


@pytest.fixture
def config() -> ServiceConfig:
    return ServiceConfig("iot-x.azure-devices.net", "https://kv.vault.azure.net/", "secret", "iotctl")


def run_cli(argv, hub: HubService, config: ServiceConfig, monitor=None):
    @contextmanager
    def factory(_config):
        yield hub

    return cli.main(
        argv, hub_factory=factory, monitor_runner=monitor or (lambda *_: None), config_loader=lambda: config
    )


def test_cli_invoke(hub, registry, config, capsys) -> None:
    code = run_cli(
        ["invoke", "press-01", "SetSamplingRate", "--payload", '{"intervalMs": 1000}'], hub, config
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"status": 200, "payload": {"ok": True}}


def test_cli_c2d_merges_type_and_data(hub, registry, config) -> None:
    code = run_cli(
        ["c2d", "send", "press-01", "--type", "maintenanceScheduled", "--data", '{"window": "Sat"}'],
        hub,
        config,
    )
    assert code == 0
    assert json.loads(registry.calls[-1][2]) == {"type": "maintenanceScheduled", "window": "Sat"}


def test_cli_twin_set(hub, registry, config) -> None:
    assert (
        run_cli(["twin", "set", "press-01", "--desired", '{"telemetryIntervalMs": 2000}'], hub, config) == 0
    )
    assert registry.calls[-1][2] == {"telemetryIntervalMs": 2000}


def test_cli_rejects_bad_json(hub, config) -> None:
    with pytest.raises(SystemExit) as exc:
        run_cli(["twin", "set", "press-01", "--desired", "[1,2]"], hub, config)
    assert exc.value.code == 2


def test_cli_operation_error_exit_code(hub, registry, config, capsys) -> None:
    registry.fail_with = _http_error(404, "DeviceNotFound")
    assert run_cli(["twin", "get", "ghost"], hub, config) == 1
    assert "HTTP 404" in capsys.readouterr().err


def test_cli_config_error_exit_code(hub, capsys) -> None:
    def broken():
        raise ConfigError("Missing required environment variable: IOTHUB_HOSTNAME")

    code = cli.main(["devices", "list"], config_loader=broken)
    assert code == 2 and "IOTHUB_HOSTNAME" in capsys.readouterr().err


def test_cli_monitor_passes_filter(hub, config) -> None:
    seen = []
    assert run_cli(["monitor", "--device", "press-01"], hub, config, monitor=lambda c, d: seen.append(d)) == 0
    assert seen == ["press-01"]


def test_cli_create_device_uses_cert_thumbprint(hub, registry, config, tmp_path: Path) -> None:
    key, cert = create_certificate("press-01", 30)
    cert_path, _ = write_files(key, cert, tmp_path, "press-01", force=False)
    assert run_cli(["devices", "create", "press-01", "--cert", str(cert_path)], hub, config) == 0
    _, device_id, primary, secondary, status = registry.calls[-1]
    assert device_id == "press-01" and primary == secondary == cli.thumbprint(cert_path)
    assert len(primary) == 64 and status == "enabled"


def test_cli_create_device_missing_cert(hub, config, capsys) -> None:
    assert run_cli(["devices", "create", "press-01", "--cert", "nope.pem"], hub, config) == 2


# ---------------------------------------------------------------- certificate tool


def test_certificate_is_self_signed_client_cert(tmp_path: Path) -> None:
    key, cert = create_certificate("press-01", 30)
    _cert_path, key_path = write_files(key, cert, tmp_path, "press-01", force=False)
    assert cert.subject == cert.issuer
    assert cert.subject.rfc4514_string() == "CN=press-01"
    cert.verify_directly_issued_by(cert)
    assert key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    with pytest.raises(FileExistsError):
        write_files(key, cert, tmp_path, "press-01", force=False)


@pytest.mark.parametrize("device_id", ["", "bad id", "a" * 129, "x/y"])
def test_certificate_rejects_invalid_device_ids(device_id: str) -> None:
    with pytest.raises(ValueError):
        create_certificate(device_id, 30)
