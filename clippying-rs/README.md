# clippying-rs

`clippying` is a background audio buffer manager that exposes a local **WebSocket JSON API** for control and status.

- WebSocket endpoint: `ws://127.0.0.1:17373/`
- Log file: `/tmp/clippying.log`

## Build

```bash
cargo build --release
```

## Run (background only)

Start the daemon in the background:

```bash
./target/release/clippying start
```

Stop it:

```bash
./target/release/clippying stop
```

Restart it:

```bash
./target/release/clippying restart
```

The daemon logs to:

```bash
tail -n 200 /tmp/clippying.log
```

Verify it is listening:

```bash
ss -ltnp | grep 17373 || true
```

## WebSocket API

All API messages are JSON.

- Requests are JSON objects with a `cmd` field.
- Responses are JSON objects with a `type` field.
- Some events are **pushed** by the daemon (not a response to a request), e.g. `clip_saved`.

### Connect with websocat

Connect:

```bash
websocat ws://127.0.0.1:17373/
```

Then paste JSON requests (one per line).

### Request: sources

Request:

```json
{"cmd":"sources"}
```

Response:

```json
{
  "type": "sources",
  "sources": [
    {
      "name": "alsa_output.usb-Elgato_Systems_Elgato_Wave_3_BS08M1A00918-00.analog-stereo.monitor",
      "description": "Monitor of Elgato Wave 3 Analog Stereo"
    },
    {
      "name": "alsa_input.usb-Elgato_Systems_Elgato_Wave_3_BS08M1A00918-00.mono-fallback",
      "description": "Elgato Wave 3 Mono"
    },
    {
      "name": "pipeweaver_voice.monitor",
      "description": "Monitor of PipeWeaver Voice"
    }
  ]
}
```

### Request: monitor

Request:

```json
{"cmd":"monitor","source":"pipeweaver_voice.monitor"}
```

Response:

```json
{"type":"ok"}
```

### Request: status

Request:

```json
{"cmd":"status"}
```

Response:

```json
{
  "type":"status",
  "statuses":[
    {
      "source":"pipeweaver_voice.monitor",
      "sample_rate":48000,
      "channels":2,
      "buffer_secs":30,
      "buffered_samples":2880000,
      "ws_port":17373,
      "last_clip":{
        "path":"/home/user/clips/clip_1765589360.wav",
        "start_secs":15.244,
        "end_secs":18.486666
      }
    }
  ]
}
```

### Request: clip

Request:

```json
{"cmd":"clip","source":"pipeweaver_voice.monitor"}
```

Response:

```json
{"type":"ok"}
```

This spawns the trimmer UI and streams PCM to it.

#### Push event: clip_saved

When you save a clip in the trimmer UI, the daemon broadcasts a push event to **all connected WebSocket clients**:

```json
{
  "type": "clip_saved",
  "source": "pipeweaver_voice.monitor",
  "path": "/home/user/clips/clip_1765589360.wav",
  "start_secs": 15.244000434875488,
  "end_secs": 18.486665725708008
}
```

### Request: stop

Request:

```json
{"cmd":"stop","source":"pipeweaver_voice.monitor"}
```

Response:

```json
{"type":"ok"}
```

### Request: stop_all

Request:

```json
{"cmd":"stop_all"}
```

Response:

```json
{"type":"ok"}
```
