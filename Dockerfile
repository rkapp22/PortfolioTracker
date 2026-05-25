# =============================================================================
# Investment Portfolio Pipeline — app image
# Python 3.12 (Debian slim, Linux) + pipeline dependencies + cron
# =============================================================================
FROM python:3.12-slim

# --- System packages ---------------------------------------------------------
# cron       : the bonus scheduler
# tini        : proper PID-1 init so signals/zombies are handled cleanly
# postgresql-client : gives us `psql` inside the container for debugging
# build deps  : psycopg2 (non-binary) would need these; we use psycopg2-binary
#               so they're not strictly required, but tzdata + locales help cron.
RUN apt-get update && apt-get install -y --no-install-recommends \
        cron \
        tini \
        postgresql-client \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# --- Timezone (cron honours this) --------------------------------------------
ENV TZ=Europe/Tallinn
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# --- Python environment ------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install requirements first (layer caching: deps only re-install when
# requirements.txt changes, not on every code edit).
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Register the bonus cron schedule (inactive unless cron is started).
COPY crontab /etc/cron.d/portfolio-cron
RUN chmod 0644 /etc/cron.d/portfolio-cron && crontab /etc/cron.d/portfolio-cron

# Entrypoint script decides: idle (manual mode) or run cron (scheduled mode).
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# NOTE: application code (./src) is BIND-MOUNTED at runtime via compose,
# not COPYed here — so students edit on the host and changes are live
# with no image rebuild. Only dependencies live in the image.

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
