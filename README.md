# Monero One Hardware

Experimental hardware-wallet integration for **Monero One**, currently targeting **Keystone** using the Feather-compatible air-gapped QR workflow.

## Security model

- Monero One runs as the online/watch-only coordinator.
- Keystone remains the offline signer and retains the recovery phrase/private spend key.
- The phone stores only the primary address and private view key for the Keystone-backed wallet.
- Outputs, key images, unsigned transactions, and signed transactions move over animated QR/UR.
- Always verify the destination, amount, and fee on Keystone before approving.

## Test flow

1. Install the debug APK from **Releases**.
2. In Monero One choose **Pair Keystone**.
3. On Keystone open the Monero / Feather connection QR and scan it.
4. Let the watch-only wallet sync.
5. Send a very small test amount.
6. Scan Monero One's outputs QR on Keystone.
7. Scan Keystone's key-images QR back into Monero One.
8. Scan Monero One's unsigned transaction on Keystone.
9. Verify destination, amount, and fee on Keystone and sign.
10. Scan the signed transaction back into Monero One for broadcast.

## Build

The GitHub Actions workflow clones the upstream `bwgh0/MoneroOne.Android` project and applies the integration in `tools/moneroone-keystone/patch.py`.

This is an experimental debug build and has not yet completed a physical end-to-end Keystone signing validation. **Do not use significant funds until the full hardware round-trip has been tested.**

## Upstream

This repository contains an integration/build harness for testing against the upstream Monero One Android project. It is not an official Monero One release.
