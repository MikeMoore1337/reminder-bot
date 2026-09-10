FROM python:3.12-slim AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

FROM runtime-base AS ffmpeg-builder

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    mkdir -p /opt/ffmpeg-runtime/usr/bin; \
    cp --parents /usr/bin/ffmpeg /opt/ffmpeg-runtime; \
    ldd /usr/bin/ffmpeg \
        | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^\//) { print $i; break } }' \
        | sort -u > /tmp/ffmpeg-libraries; \
    test -s /tmp/ffmpeg-libraries; \
    while IFS= read -r library; do \
        case "$library" in \
            /lib/*) relative="${library#/lib/}" ;; \
            /usr/lib/*) relative="${library#/usr/lib/}" ;; \
            *) continue ;; \
        esac; \
        destination="/opt/ffmpeg-runtime/usr/lib/$relative"; \
        mkdir -p "$(dirname "$destination")"; \
        cp -L "$library" "$destination"; \
    done < /tmp/ffmpeg-libraries; \
    rm -f /tmp/ffmpeg-libraries

FROM runtime-base

COPY --from=ffmpeg-builder /opt/ffmpeg-runtime/usr/bin/ffmpeg /usr/bin/ffmpeg
COPY --from=ffmpeg-builder /opt/ffmpeg-runtime/usr/lib/ /usr/lib/

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --no-compile -r requirements.txt

COPY . .

CMD ["python", "-m", "app.main"]
