"""Run the simulated press: ``python -m device``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from azure.iot.device import X509
from azure.iot.device.aio import IoTHubDeviceClient

from device.app import DeviceApp
from device.config import ConfigError, load_config
from device.machine import MachineSimulator, MachineState

logger = logging.getLogger("devctl.device")


def _install_signal_handlers(app: DeviceApp) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, app.request_stop)


async def run() -> None:
    config = load_config()
    x509 = X509(cert_file=config.cert_file, key_file=config.key_file, pass_phrase=config.key_passphrase)
    client = IoTHubDeviceClient.create_from_x509_certificate(
        x509=x509, hostname=config.hostname, device_id=config.device_id
    )
    app = DeviceApp(client, MachineSimulator(MachineState()), config.device_id)
    _install_signal_handlers(app)

    try:
        await client.connect()
        logger.info("connected to %s as %s", config.hostname, config.device_id)
        try:
            await app.run()
        finally:
            await app.shutdown()
    finally:
        await client.shutdown()
        logger.info("device client shut down")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run())
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        logger.info("interrupted")


if __name__ == "__main__":
    main()
