#!/usr/bin/env bash
# Run ON THE SERVER to ship a new backend version:  bash /srv/imax/backend/deploy/update.sh
set -euo pipefail
cd /srv/imax/backend
git pull --ff-only
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput -v 0
sudo systemctl restart imax-gunicorn imax-celery imax-celerybeat
sleep 2
systemctl is-active imax-gunicorn imax-celery imax-celerybeat
echo "Backend updated."
