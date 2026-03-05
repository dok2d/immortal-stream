# immortal-stream — fault-tolerant live streaming container
# Build: podman build -t immortal-stream .
# Run:   podman run --rm -v ./config.yaml:/etc/immortal-stream/config.yaml:ro \
#                        -v ./media:/media:ro \
#                        -p 1935:1935 -p 8890:8890/udp \
#                        immortal-stream

FROM alpine:3.21

# ── System packages ────────────────────────────────────────────────────────────
RUN apk add --no-cache \
        ffmpeg \
        python3 \
        py3-yaml \
        curl \
        ca-certificates \
        tzdata \
        fontconfig \
        unzip \
    && rm -rf /var/cache/apk/*

# ── JetBrains Mono font (supports Latin, Cyrillic, Greek) ────────────────────
ARG JBMONO_VERSION=2.304
RUN mkdir -p /usr/share/fonts/jetbrains-mono \
    && curl -fsSL \
       "https://github.com/JetBrains/JetBrainsMono/releases/download/v${JBMONO_VERSION}/JetBrainsMono-${JBMONO_VERSION}.zip" \
       -o /tmp/jbmono.zip \
    && unzip -j /tmp/jbmono.zip "fonts/ttf/*.ttf" -d /usr/share/fonts/jetbrains-mono/ \
    && rm /tmp/jbmono.zip \
    && fc-cache -f

# ── mediamtx (single Go binary, minimal footprint) ────────────────────────────
ARG MEDIAMTX_VERSION=v1.16.1
ARG TARGETOS=linux
ARG TARGETARCH=amd64
RUN curl -fsSL \
    "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_${TARGETOS}_${TARGETARCH}.tar.gz" \
    | tar -xz -C /usr/local/bin mediamtx \
    && chmod +x /usr/local/bin/mediamtx

# ── Non-root user ──────────────────────────────────────────────────────────────
RUN addgroup -g 1000 streamer \
    && adduser -u 1000 -G streamer -s /sbin/nologin -D streamer

# ── Application ───────────────────────────────────────────────────────────────
COPY src/ /app/
RUN chown -R streamer:streamer /app

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Directories ───────────────────────────────────────────────────────────────
RUN mkdir -p /etc/immortal-stream /media /tmp/immortal-stream \
    && chown -R streamer:streamer /tmp/immortal-stream

# ── Security ──────────────────────────────────────────────────────────────────
USER streamer
WORKDIR /app

# RTMP ingest (external)
EXPOSE 1935/tcp
# SRT ingest (external)
EXPOSE 8890/udp
# HLS ingest (optional, enable in config)
EXPOSE 8888/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://127.0.0.1:9997/v3/paths/list > /dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
