"""iotctl: operate devices on Azure IoT Hub with your Microsoft Entra ID identity.

    iotctl devices list | create <id> --cert <pem> | delete <id>
    iotctl twin get <id> | set <id> --desired '<json>'
    iotctl invoke <id> <method> [--payload '<json>'] [--timeout 30]
    iotctl c2d send <id> --type <type> [--data '<json>'] [--ttl 3600]
    iotctl monitor [--device <id>]

Results are printed as JSON on stdout; errors go to stderr with exit code 1
(operation failed) or 2 (invalid input or configuration).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from azure.core.exceptions import AzureError, ClientAuthenticationError
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from service.config import ConfigError, ServiceConfig, load_config
from service.hub import HubError, HubService

logger = logging.getLogger("devctl.iotctl")

HubFactory = Callable[[ServiceConfig], AbstractContextManager[HubService]]
MonitorRunner = Callable[[ServiceConfig, str | None], None]


def _json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return value


def _json_value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc}") from exc


def thumbprint(cert_path: Path) -> str:
    """SHA-256 thumbprint of a PEM certificate, as IoT Hub expects it (uppercase hex)."""
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex().upper()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iotctl", description="Operate devices on Azure IoT Hub.")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    groups = parser.add_subparsers(dest="group", required=True)

    devices = groups.add_parser("devices", help="device identities").add_subparsers(
        dest="action", required=True
    )
    listing = devices.add_parser("list", help="list devices")
    listing.add_argument("--limit", type=int, default=100)
    create = devices.add_parser("create", help="register a self-signed X.509 device")
    create.add_argument("device_id")
    create.add_argument("--cert", type=Path, required=True, help="device certificate (PEM)")
    create.add_argument("--secondary-cert", type=Path, help="rollover certificate (PEM)")
    delete = devices.add_parser("delete", help="delete a device")
    delete.add_argument("device_id")

    twin = groups.add_parser("twin", help="device twin").add_subparsers(dest="action", required=True)
    twin.add_parser("get", help="show a twin").add_argument("device_id")
    twin_set = twin.add_parser("set", help="patch desired properties")
    twin_set.add_argument("device_id")
    twin_set.add_argument("--desired", type=_json_object, required=True, help="JSON object to merge")

    invoke = groups.add_parser("invoke", help="call a direct method")
    invoke.add_argument("device_id")
    invoke.add_argument("method")
    invoke.add_argument("--payload", type=_json_value, default=None, help="JSON payload")
    invoke.add_argument("--timeout", type=int, default=30, help="response timeout in seconds")

    c2d = groups.add_parser("c2d", help="cloud-to-device messages").add_subparsers(
        dest="action", required=True
    )
    send = c2d.add_parser("send", help="queue a C2D message")
    send.add_argument("device_id")
    send.add_argument(
        "--type", required=True, dest="message_type", help="message type, e.g. maintenanceScheduled"
    )
    send.add_argument("--data", type=_json_object, default={}, help="extra JSON fields")
    send.add_argument("--ttl", type=int, help="expiry in seconds")

    monitor = groups.add_parser("monitor", help="stream device telemetry")
    monitor.add_argument("--device", help="only show this device")
    return parser


@contextmanager
def _default_hub(config: ServiceConfig) -> Iterator[HubService]:
    from azure.identity import DefaultAzureCredential

    with DefaultAzureCredential() as credential, HubService.connect(config.hostname, credential) as hub:
        yield hub


def _default_monitor(config: ServiceConfig, device: str | None) -> None:
    from azure.identity import DefaultAzureCredential

    from service.monitor import TelemetryMonitor, fetch_connection_string

    with DefaultAzureCredential() as credential:
        connection = fetch_connection_string(
            config.require_key_vault(), config.telemetry_secret_name, credential
        )
    TelemetryMonitor(connection, config.consumer_group, device).run()


def _execute(args: argparse.Namespace, hub: HubService) -> Any:
    if args.group == "devices":
        if args.action == "list":
            return hub.list_devices(args.limit)
        if args.action == "create":
            primary = thumbprint(args.cert)
            secondary = thumbprint(args.secondary_cert) if args.secondary_cert else primary
            return hub.register_x509_device(args.device_id, primary, secondary)
        hub.delete_device(args.device_id)
        return {"deleted": args.device_id}
    if args.group == "twin":
        if args.action == "get":
            return hub.get_twin(args.device_id)
        return {"desired": hub.set_desired(args.device_id, args.desired)}
    if args.group == "invoke":
        result = hub.invoke_method(args.device_id, args.method, args.payload, args.timeout)
        return {"status": result.status, "payload": result.payload}
    hub.send_c2d(args.device_id, {"type": args.message_type, **args.data}, args.ttl)
    return {"queued": True, "deviceId": args.device_id, "type": args.message_type}


def main(
    argv: Sequence[str] | None = None,
    hub_factory: HubFactory = _default_hub,
    monitor_runner: MonitorRunner = _default_monitor,
    config_loader: Callable[[], ServiceConfig] = load_config,
) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = config_loader()
        if args.group == "monitor":
            monitor_runner(config, args.device)
            return 0
        with hub_factory(config) as hub:
            result = _execute(args, hub)
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ClientAuthenticationError as exc:
        print(f"error: not signed in to Azure ({exc.message}); run 'az login'", file=sys.stderr)
        return 1
    except (HubError, AzureError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, indent=2, default=str))
    return 0


def console() -> None:
    raise SystemExit(main())
