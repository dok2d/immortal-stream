# immortal-stream

Fault-tolerant live streaming relay with 3-layer compositing, designed to keep the connection to target services (YouTube, Twitch, Restream, etc.) alive at all times — even when the incoming stream is absent, unstable, or in an unknown format.

---

## How it works

```
 Primary stream  ─┐
 Backup stream   ─┤  RTMP / RTSP / SRT / HLS
 Emergency stream─┘
        │
        ▼
  ┌─────────────┐  API poll   ┌──────────────────────┐
  │  mediamtx   │────────────▶  Python orchestrator  │
  │  (ingest)   │             └──────────┬───────────┘
  └─────────────┘                        │ priority selection
                                         │ + failover
                                         ▼
                              ┌────────────────────────┐
  Placeholder ──────────────▶ │   Compositor FFmpeg    │
  Overlay     ──────────────▶ │   (3-layer composite)  │
                              └──────────┬─────────────┘
                                         │ internal relay
                                         ▼
                              ┌────────────────────────┐
                              │  Output FFmpeg         │  ──▶  YouTube
                              │  (never restarts)      │  ──▶  Twitch
                              └────────────────────────┘  ──▶  …
```

### Layers

| Priority | Layer | Visibility |
|----------|-------|-----------|
| Base | **Placeholder** | Always sent to the target service. Shown as-is when no incoming stream is active. Keeps the broadcast alive. Can be: black screen, testcard (colour bars + clock), text, image, or looping video. |
| Middle | **Incoming stream** | Any protocol and codec accepted (RTMP, RTSP, SRT, HLS). Replaces the placeholder while active. |
| Top | **Overlay** | Image or text composited over the incoming stream. Shown **only** when a stream is active. |

### Continuous output guarantee

The output FFmpeg process connects to the target service once at startup and **never disconnects**. A persistent internal relay (mediamtx) buffers the compositor output, so brief compositor restarts (< 2 s) during stream switching are invisible to the target service.

### Redundant input sources

Multiple input sources can be configured with priority ordering. The compositor always uses the highest-priority active stream. When that stream drops, the system instantly fails over to the next available source — the RTMP connection to YouTube/Twitch is uninterrupted. When a higher-priority source reconnects, it is immediately promoted back.

---

## Quick start

### 1. Build the image

```sh
podman build -t immortal-stream .
```

For ARM hosts (Raspberry Pi 4, Apple Silicon VM, etc.):

```sh
podman build --build-arg TARGETARCH=arm64 -t immortal-stream .
```

### 2. Create your config

```sh
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Mount any media files (placeholder image/video, overlay image, fonts) into `/media` inside the container.

### 3. Run

```sh
podman run -d --name immortal-stream \
  --restart unless-stopped \
  -v "$(pwd)/config.yaml:/etc/immortal-stream/config.yaml:ro" \
  -v "$(pwd)/media:/media:ro" \
  -p 1935:1935/tcp \
  -p 8890:8890/udp \
  immortal-stream
```

Or with Compose:

```sh
podman-compose up -d
# or
docker compose up -d
```

| Port | Protocol | Purpose |
|------|----------|---------|
| 1935 | TCP | RTMP / RTSP ingest |
| 8890 | UDP | SRT ingest |
| 8888 | TCP | HLS ingest (optional, disabled by default) |

---

## Configuration reference

All options live in a single YAML file. See [`config.example.yaml`](config.example.yaml) for a fully annotated example.

### `ingest`

```yaml
ingest:
  port: 1935               # RTMP/RTSP ingest port
  srt_port: 8890           # SRT ingest port
  hls: false               # Enable HLS ingest (port 8888)
  hls_port: 8888           # HLS ingest port
  stream_key_required: false
  allowed_key: ""          # only used when stream_key_required: true
```

When `stream_key_required: true`, only streams published to `rtmp://host:1935/live/<allowed_key>` are accepted. Streams on any other path are ignored.

### `ingest.redundant_sources` — redundant input with automatic failover

```yaml
ingest:
  redundant_sources:
    - primary    # rtmp://host:1935/live/primary   (highest priority)
    - backup     # rtmp://host:1935/live/backup
    - emergency  # rtmp://host:1935/live/emergency (lowest priority)
```

When `redundant_sources` is set, the orchestrator tracks all listed sources simultaneously and always composites the **highest-priority source that is currently connected**:

- All sources can be connected at the same time. Lower-priority ones stay on standby and do not consume compositor resources.
- If the active source disconnects, the system **instantly fails over** to the next available source — the output FFmpeg process never restarts and the RTMP connection to YouTube/Twitch is uninterrupted.
- When a higher-priority source reconnects, it is **immediately promoted** back to the compositor.

Telegram notifications report every standby connect/disconnect, every preemption, and every failover event with remote IP, protocol, and codec details.

Leave `redundant_sources` empty (default) to accept any single stream on `/live/*` (legacy first-come behaviour).

### `placeholder`

```yaml
placeholder:
  type: testcard           # black | testcard | text | image | video
  timezone: UTC            # IANA tz name for testcard clock (e.g. Europe/Moscow)
  # Text placeholder:
  # text: "Stream starting soon"
  # font_path: /media/fonts/DejaVuSans.ttf
  # font_size: 72
  # font_color: white
  # Image/video placeholder:
  # path: /media/holder.jpg  # required for image/video
  x: 0                      # position (text: 0,0 = centered)
  y: 0
  opacity: 1.0
```

The placeholder is re-encoded to the configured output resolution. `testcard` shows colour bars with a live clock overlay. Images are padded with black bars to maintain aspect ratio. Videos loop seamlessly.

### `overlay`

```yaml
overlay:
  enabled: true
  type: image              # image | text
  path: /media/logo.png    # for type: image (PNG/JPEG)
  # text: "LIVE"           # for type: text
  # font_path: /media/font.ttf
  # font_size: 48
  # font_color: white
  x: 20
  y: 20
  opacity: 0.9
```

The overlay is composited **only** while an incoming stream is active.

### `output`

```yaml
output:
  targets:
    - rtmp://a.rtmp.youtube.com/live2/YOUR_KEY
    - rtmp://live.twitch.tv/app/YOUR_KEY
  video:
    width: 1920
    height: 1080
    fps: 30
    bitrate: 6000k
    preset: ultrafast      # x264 preset; ultrafast = lowest CPU
    gop: 60
  audio:
    bitrate: 128k
    sample_rate: 44100
```

Multiple targets are sent simultaneously via FFmpeg's tee muxer. All use `copy` codec, so re-encoding happens only in the compositor, not per-target.

### `telegram`

```yaml
telegram:
  enabled: true
  bot_token: "123456:ABC..."
  chat_id: "-1001234567890"
```

Events reported: stream started (with remote IP / protocol / codec / resolution / FPS), stream stopped (with duration), failover, priority preemption, process restarts, errors.

Bot commands for runtime configuration:

| Command | Description |
|---------|-------------|
| `/status` | Current stream state and settings |
| `/placeholder black\|testcard\|text\|image\|video\|opacity\|timezone` | Change placeholder |
| `/overlay off\|text\|image\|x\|y\|opacity\|size\|color` | Configure overlay |
| `/target list\|add\|remove\|set` | Manage output RTMP targets |
| `/output bitrate\|fps\|size\|preset` | Change output encoding |
| `/help` | Show available commands |

---

## Incoming stream sources

Connect your encoder or source to:

| Protocol | URL format |
|----------|-----------|
| RTMP | `rtmp://host:1935/live` or `rtmp://host:1935/live/<key>` |
| RTSP | `rtsp://host:1935/live` |
| SRT | `srt://host:8890` |
| HLS | `http://host:8888/live` (when enabled) |

Any codec and resolution are accepted; the compositor re-encodes to the configured output parameters. Audio is optional — silence is generated if the source has no audio track.

---

## Security considerations

- The container runs as a **non-root user** (uid 1000).
- The internal RTMP relay path is protected by a randomly generated token, regenerated on each container start.
- Config and media files are mounted **read-only**.
- Only ingest ports are exposed externally. All internal components communicate over the loopback interface.
- No outbound connections other than to configured RTMP targets and the Telegram API (if enabled).
- Security hardening in compose.yaml: `no-new-privileges`, `cap_drop: ALL`, `tmpfs` for temporary files.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_PATH` | `/etc/immortal-stream/config.yaml` | Path to config file inside container |
| `LOG_LEVEL` | `INFO` | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Troubleshooting

**Stream is accepted but output is black / frozen**
- Check that the placeholder file path is correct and readable inside the container.
- Run with `LOG_LEVEL=DEBUG` to see FFmpeg command output.

**Publisher is rejected / not detected**
- `stream_key_required: true` is set. Connect to `rtmp://host:1935/live/<allowed_key>`.
- When using `redundant_sources`, connect to `rtmp://host:1935/live/<source_name>`.

**Output FFmpeg exits immediately**
- Verify that `output.targets` URLs are reachable from the container.
- Check stream key validity with the target service.

**High CPU usage**
- Set `preset: ultrafast` (default). Reduce resolution or frame rate if needed.
- Ensure the placeholder video is already in the correct resolution to avoid expensive scaling.

**Viewing logs**
```sh
podman logs -f immortal-stream
```

---

## Requirements

| Component | Version |
|-----------|---------|
| Podman | >= 4.0 |
| Docker | >= 24 (alternative) |

The image includes FFmpeg, mediamtx, and Python 3 — no other dependencies needed on the host.
