# ADR 0003: Read telemetry from the built-in endpoint with a key held in Key Vault

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

The operator needs to watch device-to-cloud telemetry. IoT Hub's built-in Event
Hubs-compatible endpoint **does not support Microsoft Entra ID**; it accepts only a
shared-access key. Reading with Entra ID requires routing messages to a custom Event Hubs
namespace, which adds a billed resource (Event Hubs is not free) and routing configuration.

## Decision

- Read from the **built-in endpoint** using the hub's least-privileged policy, `service`
  (ServiceConnect only).
- Bicep writes that connection string into **Key Vault** (RBAC mode, soft delete). `iotctl monitor`
  fetches it at runtime with the operator's Entra ID identity (*Key Vault Secrets User*). It is never
  written to disk, `.env`, or source control.
- `iotctl monitor` reads through a **dedicated consumer group** (`iotctl`), so it never competes with
  other readers on `$Default`.

## Consequences

- Zero added cost and no secret on any laptop or in git; access is revocable by removing one role
  assignment.
- The key itself is long-lived; anyone holding *Key Vault Secrets User* can read it. Rotate by
  regenerating the `service` policy key and redeploying.
