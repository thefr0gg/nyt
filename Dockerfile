FROM python:3.15-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git curl unzip \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JS runtime to reliably extract YouTube formats and avoid bot detection
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

# Copy source and install — pyproject.toml declares all Python deps
COPY pyproject.toml README.md ./
COPY nyt/ ./nyt/

RUN pip install --no-cache-dir .

# Persist config, database, videos, and avatars across restarts
VOLUME ["/root/.nyt"]

EXPOSE 9473

# Bind to all interfaces so the port mapping works outside the container.
# Override --delay (watcher interval in minutes) and --port as needed.
CMD ["nyt", "serve", "--host", "0.0.0.0", "--port", "9473"]
