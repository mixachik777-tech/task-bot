#!/bin/sh
set -e

echo "Running migrations..."
alembic upgrade head
echo "Current revision: $(alembic current 2>&1 | tail -1)"
echo "Running seeders..."
python -m app.db.seeders
echo "Starting bot..."
exec python -m app.main
