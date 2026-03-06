# Clippying

Clippying is an audio clipping toolkit made of:

- `clippying-rs`: a Rust daemon that buffers monitor audio and exposes a local WebSocket JSON API.
- `clippying-sc-plugin`: a StreamController plugin that controls the daemon and saves clips from a button action.

## Repository Layout

- `clippying-rs/` - core daemon + clip trimmer UI
- `clippying-sc-plugin/` - StreamController plugin + native Rust bridge

## How It Works

1. The daemon runs locally and listens on `ws://127.0.0.1:17373/`.
2. A client (for example the StreamController plugin) sends JSON commands like `sources`, `monitor`, and `clip`.
3. When a clip is saved, the daemon broadcasts a `clip_saved` event to connected clients.

## Requirements

- Linux audio stack with monitor sources available
- Rust toolchain (`cargo`) for building Rust components
- StreamController `>= 1.5.0-beta.12` (for plugin usage)
- Python dependency for plugin: `websocket-client`
- Optional playback tools for plugin long-press: `paplay` or `aplay`

## Quick Start

### 1. Build and run the daemon

```bash
cd clippying-rs
cargo build --release
./target/release/clippying start
```

Check daemon log output:

```bash
tail -n 200 /tmp/clippying.log
```

Stop or restart daemon:

```bash
./target/release/clippying stop
./target/release/clippying restart
```

### 2. Build plugin native bindings

```bash
cd ../clippying-sc-plugin
./build.sh release
```

Dev build:

```bash
./build.sh dev
```

Clean artifacts:

```bash
./build.sh clean
```

## Daemon API (Summary)

WebSocket endpoint: `ws://127.0.0.1:17373/`

Common requests:

- `{"cmd":"sources"}` - list available monitor sources
- `{"cmd":"monitor","source":"..."}` - start monitoring source
- `{"cmd":"status"}` - get monitoring/buffer status
- `{"cmd":"clip","source":"..."}` - open trimmer and create clip
- `{"cmd":"stop","source":"..."}` - stop one source
- `{"cmd":"stop_all"}` - stop all monitored sources

Push event:

- `clip_saved` - sent to all connected clients when a clip is saved

## StreamController Action (Summary)

The plugin provides a `Clip Button` action:

- Lets you choose a monitor source from daemon `sources`
- Auto-starts the daemon if API is unavailable
- Auto-enables monitoring for selected source
- Short press triggers `clip` and waits for matching `clip_saved`
- Long press replays the last saved clip

## Detailed Component Docs

- Daemon details: `clippying-rs/README.md`
- Plugin details: `clippying-sc-plugin/README.md`

## Development Notes

- Plugin manifest ID: `com_designgears_clippying`
- Plugin metadata and compatibility live in `clippying-sc-plugin/manifest.json`
- Default daemon log file: `/tmp/clippying.log`
