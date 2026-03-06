# Clippying StreamController Plugin

This is a StreamController plugin that provides a single action to control the `clippying` Rust daemon via its WebSocket JSON API.
The plugin includes a Rust binding layer (`clippying_native`) that embeds and hosts the Rust side from `_native.abi3.so`.

## Actions

- Clip Button
  - Configure a **Monitor source** (pulled from `{"cmd":"sources"}`)
  - Automatically starts the daemon if the WebSocket API is not reachable
  - Automatically enables monitoring for the selected source
  - Short press triggers `{"cmd":"clip","source":"..."}` and waits for the matching `clip_saved` event
  - Long press plays the last saved clip for that button

## Requirements

- StreamController >= `1.5.0-beta.12`
- Python dependency: `websocket-client` (see `requirements.txt`)
- For long-press playback: `paplay` or `aplay` available on the system
- For building bindings: Rust toolchain (`cargo`)

## Build

Build Rust bindings and place them in the plugin package:

```bash
./build.sh release
```

Dev build:

```bash
./build.sh dev
```

Clean build artifacts:

```bash
./build.sh clean
```

## Notes

- WebSocket URL defaults to `ws://127.0.0.1:17373`.
- The plugin continuously heartbeats the API and will attempt to become host if it disappears.
