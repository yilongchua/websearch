FROM python:3.12-slim-bookworm

ARG SEARXNG_GIT_REF=master
ARG CRAWL4AI_PRE_RELEASE=false

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WEBSEARCH_CONFIG_PATH=/app/config.yaml

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    git \
    libffi-dev \
    libssl-dev \
    libxslt1-dev \
    tini \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Local SearXNG installation in this container.
RUN git clone --depth 1 --branch "$SEARXNG_GIT_REF" https://github.com/searxng/searxng.git /opt/searxng-src \
    && mkdir -p /etc/searxng \
    && cp /opt/searxng-src/searx/settings.yml /etc/searxng/settings.yml \
    && python -m venv /opt/searxng-venv \
    && /opt/searxng-venv/bin/pip install -U pip setuptools wheel pyyaml msgspec typing-extensions pybind11 \
    && /opt/searxng-venv/bin/pip install --use-pep517 --no-build-isolation -e /opt/searxng-src \
    && /opt/searxng-venv/bin/pip install -r /opt/searxng-src/requirements-server.txt

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Local Crawl4AI installation (+ setup/doctor as requested).
RUN if [ "$CRAWL4AI_PRE_RELEASE" = "true" ]; then \
      pip install crawl4ai --pre; \
    else \
      pip install -U crawl4ai; \
    fi \
    && crawl4ai-setup \
    && (crawl4ai-doctor || true)

COPY main.py /app/main.py
COPY schema /app/schema
COPY utils /app/utils
COPY prompt /app/prompt
COPY config.yaml /app/config.yaml
COPY searxng-settings.yml /etc/searxng/settings.yml
COPY entrypoint.sh /app/entrypoint.sh

RUN mkdir -p /etc/searxng /app/output \
    && chmod +x /app/entrypoint.sh

EXPOSE 8080 9000

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
