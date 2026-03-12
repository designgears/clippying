# Clippying StreamController Plugin

This is a StreamController plugin that provides capture and playback actions for the `clippying` Rust daemon via its WebSocket JSON API.
The plugin includes a Rust binding layer (`clippying_native`) that embeds and hosts the Rust side from `_native.abi3.so`.

## Actions

- Capture Button
  - Configure a **Monitor source** (pulled from `{"cmd":"sources"}`)
  - Automatically starts the daemon if the WebSocket API is not reachable
  - Automatically enables monitoring for the selected source
  - Short press triggers `{"cmd":"clip","source":"...","clips_dir":"..."}` and waits for the matching `clip_saved` event
  - Saves clips to `<clips_dir>/<source>/latest.wav` plus timestamped archive copies

- Latest Clip Button
  - Configure the same **Monitor source** as the capture button
  - Plays the deterministic `latest.wav` for that source
  - Flashes when a fresh clip is saved for that source
  - Supports `Play / Stop`, `Play / Restart`, `Play / Overlap`, `Loop / Stop`, and `Hold To Play`

- Audio File Player
  - Plays a selected file or a random file from a selected folder
  - Can open the Rust trimmer as a range editor for a selected file and store playback start/end times
  - Supports output-device selection and the same playback modes as the latest-clip action

## Requirements

- StreamController >= `1.5.0-beta.12`
- Python dependency: `websocket-client` (see `requirements.txt`)
- For playback: `ffplay`, `paplay`, or `aplay` available on the system
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
- Plugin settings control the clips directory and trimmer preview output.
- The plugin continuously heartbeats the API and will attempt to become host if it disappears.
