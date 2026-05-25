#!/bin/bash
# =============================================================================
# Entrypoint — decides how the app container behaves based on RUN_MODE.
#   manual (default): container starts and idles. You trigger runs yourself with
#                     `docker compose exec app python src/run_pipeline.py`.
#   cron            : starts the cron daemon in the foreground so the bonus
#                     schedule in /etc/cron.d/portfolio-cron fires automatically.
# =============================================================================
set -euo pipefail

mkdir -p /app/logs

echo "-------------------------------------------------------------"
echo " Portfolio pipeline container starting"
echo " RUN_MODE = ${RUN_MODE:-manual}"
echo " TZ       = ${TZ:-UTC}"
echo "-------------------------------------------------------------"

if [ "${RUN_MODE:-manual}" = "cron" ]; then
    echo "[entrypoint] Starting cron daemon (scheduled mode)."
    # Pass current env to cron jobs (cron normally runs with a bare environment).
    printenv | grep -E '^(POSTGRES_|EXCEL_PATH|BASE_CURRENCY|TZ)' \
        | sed 's/^/export /' > /app/cron.env
    # Run cron in the foreground so it becomes the container's main process.
    # Logs are tailed so `docker compose logs app` shows schedule output.
    touch /app/logs/cron.log
    cron
    echo "[entrypoint] cron started; tailing /app/logs/cron.log"
    exec tail -f /app/logs/cron.log
else
    echo "[entrypoint] Manual mode. Container is idle and ready."
    echo "[entrypoint] Trigger a run from the host with:"
    echo "    docker compose exec app python src/run_pipeline.py"
    # Keep the container alive without consuming CPU.
    exec tail -f /dev/null
fi
