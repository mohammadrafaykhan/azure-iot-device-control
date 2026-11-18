# ADR 0002: Python service SDK (`azure-iot-hub`) behind a single adapter

- **Status:** Accepted, with a known dependency risk
- **Date:** 2026-11-17

## Context

The operator CLI must manage device identities, read and patch twins, invoke direct methods,
and send cloud-to-device (C2D) messages. Twins, methods, and the registry are available over
IoT Hub's REST API, but **sending C2D messages requires AMQP**, which means a service SDK.

Findings when this decision was made:

- `azure-iot-hub` 2.7.0 (Aug 2025) is the current Python service SDK. It supports Microsoft
  Entra ID through `IoTHubRegistryManager.from_token_credential`.
- It depends on **`uamqp`**, which Microsoft has deprecated (last release 1.6.11, Oct 2024). Wheels
  exist for CPython 3.8–3.13.
- It imports **`six` without declaring it**, so a clean install fails on import.
- Its AMQP connection is closed only in `__del__`, so release is not deterministic.
- The .NET service SDK (`Microsoft.Azure.Devices`) is actively developed (v2 RC, Apr 2026).

## Decision

- Use **`azure-iot-hub` with Entra ID only**. No shared-access policy is used for registry,
  twin, method, or C2D operations; the operator holds the *IoT Hub Data Contributor* role.
- Confine the SDK to **one module, `service/hub.py`**. Everything else depends on `HubService`.
- Pin `six` explicitly, and close the AMQP connection deterministically (`HubService` is a context
  manager).
- Translate SDK errors into a single `HubError` with an operator-safe message.

## Consequences

- One language (Python) across device and service, with no shared keys on the service plane.
- Exposure to an unmaintained transitive dependency. CI tests on Python 3.12 and 3.13 to catch
  breakage early.
- **Migration path:** if `uamqp` stops installing, replace `service/hub.py` with either a .NET
  service or REST calls for twins/methods/registry plus a maintained AMQP client for C2D. The CLI,
  tests, and callers do not change.
