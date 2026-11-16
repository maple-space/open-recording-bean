# Recording Bean Audio 2ch 1.0.1

Independent audio loopback driver distributed by the open-recording-bean project.
This product is not an official release of, endorsed by, or affiliated with
Existential Audio Inc.

## Copyright and source provenance

Derived from BlackHole v0.7.1 by Existential Audio Inc.
Original code copyright (C) 2019 Existential Audio Inc.; upstream copyright notice
and the complete GPL-3.0 license, including its branding notice, are retained in
LICENSE. References to the upstream name here identify provenance and licensing,
not the name or branding of this modified product.

- Upstream source: https://github.com/ExistentialAudio/BlackHole/tree/v0.7.1
- Original source archive: https://codeload.github.com/ExistentialAudio/BlackHole/tar.gz/refs/tags/v0.7.1
- Archive SHA-256: `e9de179da54ed55ff27876990f3a2dcfefa66e6bd6cfcba448a8564eabdf3e89`
- Changes dated 2026-11-16 by open-recording-bean contributors.

## Changes

- Product/device name: **Recording Bean Audio 2ch**.
- Executable and source symbols: `RecordingBeanAudio`.
- Bundle/package ID: `io.github.maple-space.RecordingBeanAudio2ch`.
- Device UID: `RecordingBeanAudio2ch_UID`; model UID uses the same new prefix.
- Factory UUID: `005aef35-afbf-5794-9404-d56f0d8bae3c`.
- Manufacturer field: `open-recording-bean contributors`.
- Device and Box transport: `kAudioDeviceTransportTypeUnknown`, to avoid the
  `(Virtual)` label filtered from the microphone picker in Typeless 2.11.0.
- No custom device icon property is exposed; macOS uses its default icon.
  No upstream logo, artwork, branded installer layout or icon is shipped.
- All PCM processing, buffering, formats and channel behavior remain derived
  from the unchanged upstream implementation; this is not a clean-room rewrite.

The complete corresponding source needed for this version is
`RecordingBeanAudio.c`, `Info.plist`, `VERSION`, `LICENSE`, this notice, and the
build/test scripts under `../../tools/recording-bean-audio`. They build without
upstream artwork, Xcode project files or installer scripts. System SDK headers
and frameworks are supplied by Apple developer tools.

## Distribution

Driver code remains GPL-3.0; project-specific build and probe scripts are
GPL-3.0-or-later. Publish the generated complete source archive and license
alongside the installer. Renaming does not change the source-code license.

The driver installs under its own `RecordingBeanAudio2ch.driver` directory.
It neither overwrites other drivers nor changes the default microphone.
Select **Recording Bean Audio 2ch** explicitly in the webpage and dictation app.

Local builds use ad-hoc driver signing and an unsigned installer. The builder
also accepts the publisher's own Developer ID Application/Installer identities
and an existing notarization keychain profile. No original vendor certificate,
signature or notarization is reused. See the release's verification.json for the
actual signing and notarization status; do not claim notarization if it is false.

Version 1.0.1 adds a read-only CoreAudio device checker and a single install-and-activate entry point. It checks availability first, and reloads only the named system audio service after explicit user confirmation if the device has not loaded. No computer reboot or user-app restart is performed. The audio engine is unchanged.
