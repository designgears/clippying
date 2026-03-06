# Clippying StreamController Plugin

This is a StreamController plugin that provides a single action to control the `clippying` Rust daemon via its WebSocket JSON API.

## Actions

- Clip Button
  - Configure a **Monitor source** (pulled from `{"cmd":"sources"}`)
  - Automatically starts the daemon if the WebSocket API is not reachable
  - Automatically enables monitoring for the selected source
  - Short press triggers `{"cmd":"clip","source":"..."}` and waits for the matching `clip_saved` event
  - Long press plays the last saved clip for that button

## Requirements

- StreamController >= `1.5.0-beta.12`
- `clippying` binary available on `PATH` (or set the per-action setting **Clippying binary**)
- Python dependency: `websocket-client` (see `requirements.txt`)
- For long-press playback: `paplay` or `aplay` available on the system

## Notes

- WebSocket URL defaults to `ws://127.0.0.1:17373`.
- The per-action configuration screen lets you set the WebSocket URL and `clippying` binary path.
