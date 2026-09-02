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
{"cmd":"monitor","source":"pipeweaver_voice.monitor","gain_db":0}
```

`gain_db` is optional (default `0`, range `-30` to `+30`) and boosts audio as it
enters the rolling buffer. Sending `monitor` for a source that is already
running just updates its gain.

Response:

```json
{"type":"ok"}
```

### Request: set_gain

Change the capture boost of running workers without restarting them. An empty
or omitted `source` applies the gain to every worker.

Request:

```json
{"cmd":"set_gain","source":"pipeweaver_voice.monitor","gain_db":6}
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
      "gain_db":0.0,
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
{"cmd":"clip","source":"pipeweaver_voice.monitor","gain_db":0}
```

Response:

```json
{"type":"ok"}
```

This spawns the trimmer UI and streams PCM to it. `gain_db` is the boost the
trimmer opens with; it stays adjustable there with the boost slider, and the
value that is finally used is reported back in `clip_saved`.

#### Push event: clip_saved

When you save a clip in the trimmer UI, the daemon broadcasts a push event to **all connected WebSocket clients**:

```json
{
  "type": "clip_saved",
  "source": "pipeweaver_voice.monitor",
  "path": "/home/user/clips/clip_1765589360.wav",
  "start_secs": 15.244000434875488,
  "end_secs": 18.486665725708008,
  "gain_db": 6.0
}
```

## Trimmer boost

The trimmer window has a boost slider (-30 to +30 dB) next to the transport
buttons:

- The waveform rescales instantly and turns red where the boost clips, with a
  `CLIP` badge when the current selection would hit the ceiling
- Preview playback follows the slider while it is playing
- The saved WAV is written with the boost baked in
- Clicking the dB readout returns to 0 dB

It can also be set from the command line when driving the trimmer directly:

```bash
clippying --stdin-pcm 48000 1 --gain 6 < audio.pcm
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
