# Security and privacy

This project is designed to keep the clock USB-only. Do not add Wi-Fi
credentials, cloud tokens, or account credentials to the firmware.

Before contributing or publishing, run `git status --ignored --short` and make
sure these remain local only:

- `tools/usage-collector.toml` and `tools/usage-collector-state.json`
- `tools/.bin/` and any Keychain-derived helper
- firmware images, especially an original-device flash backup
- `.env*`, private keys, and generated build output

The provider adapters may read an existing local OAuth credential only in
process memory to obtain a percentage. Never commit, print, attach, or paste a
credential, private configuration, collector state, device backup, or log in an
issue. If a secret is committed, revoke it first, then remove it from Git
history before making the repository public.

The browser control panel listens only on `127.0.0.1`. Keep it loopback-only.

To report a vulnerability, contact the repository owner privately rather than
opening a public issue with exploit details or sensitive data.
