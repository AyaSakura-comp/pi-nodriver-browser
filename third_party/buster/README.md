# Buster browser extension

This directory vendors the official Chrome release of
[Buster: Captcha Solver for Humans](https://github.com/dessant/buster) so the
managed Pi Chrome profile can load it as an unpacked Manifest V3 extension.

- Upstream version: `v3.4.0`
- Release date: 2026-06-20
- Release asset: `buster_captcha_solver_for_humans-3.4.0-chrome.zip`
- Upstream release: <https://github.com/dessant/buster/releases/tag/v3.4.0>
- Chrome release SHA-256: `26749705f1bb57ef3e4cda9aa73aa66cc71a8d9df2906c9600eaed98f0d54129`
- Corresponding source: `buster-3.4.0-source.tar.gz`
- Source tag commit: `ed6a8ce9619b833413f397d539824fbc1c5ae5ea`
- Source SHA-256: `284757ddcd82cbaf455b1d0259e09eba28112e5b7c2e708b39f47b55ebf4b1e`
- License: GNU GPL v3.0; see `LICENSE`

To build the GPL-covered code from source, extract the source archive, run
`npm ci`, then `npm run build:prod:zip:chrome`. See the upstream `package.json`
and `gulpfile.js` for the build definition. The official release ZIP also
contains an encrypted `secrets.txt` with upstream-managed speech-service
configuration that is not present in the public source tag, so this command
builds an equivalent extension without those managed credentials rather than a
byte-identical copy. Verify the redistributed official asset with the pinned
SHA-256 above.

`INSTALL_BUSTER=1 ./install.sh` explicitly opts in. The installer verifies the
checksum, rejects unsafe archive entries, extracts
the extension to `chrome-extensions/buster/` in the deployed Pi extension, and
registers it in the managed Chrome profile through pipe-only CDP
`Extensions.loadUnpacked`.
Buster supports Google reCAPTCHA audio challenges; it is not a generic solver
for hCaptcha, Cloudflare Turnstile, or proprietary bot-block pages. Its
manifest requests broad privileges including `<all_urls>`, `webRequest`,
`nativeMessaging`, and unrestricted extension-page network access. Pi never
activates Buster automatically.
