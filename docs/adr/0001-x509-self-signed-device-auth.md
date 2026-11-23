# ADR 0001: Self-signed X.509 device authentication, device SAS disabled

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Devices must authenticate to IoT Hub. The options are symmetric keys (SAS), self-signed
X.509 certificates (thumbprint), or CA-signed X.509 certificates, usually onboarded through
the Device Provisioning Service (DPS). This project demonstrates device control for a
handful of devices; fleet onboarding is the subject of a separate project.

## Decision

- Devices authenticate with **self-signed X.509 certificates** (ECDSA P-256), registered by
  SHA-256 thumbprint. The private key never leaves the device.
- The hub sets **`disableDeviceSAS` and `disableModuleSAS`**, so a leaked or guessed shared key
  cannot be used to impersonate a device.
- Each device can register a **secondary thumbprint**, which allows zero-downtime certificate
  rollover: issue the new certificate, register it as secondary, switch the device, then promote it.

## Consequences

- No shared secrets exist for devices; compromise of one device's key affects only that device.
- Thumbprints are registered one device at a time (`iotctl devices create`). This does not scale
  to fleets, and certificate expiry must be tracked per device.
