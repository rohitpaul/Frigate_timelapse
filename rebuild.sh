#!/bin/bash
# Force rebuild script for Frigate Timelapse

echo "=== Stopping and removing old container ==="
docker-compose down

echo "=== Removing old image ==="
docker rmi frigate_timelapse-frigate-timelapse 2>/dev/null || true
docker rmi frigate-timelapse 2>/dev/null || true

echo "=== Clearing build cache ==="
docker builder prune -f

echo "=== Checking git status ==="
git status

echo "=== Pulling latest code ==="
git pull rohit dev || git fetch rohit dev && git reset --hard rohit/dev

echo "=== Verifying code update ==="
if grep -q "get_auth_headers" app.py; then
    echo "✓ Code updated successfully"
else
    echo "✗ Code NOT updated - manual check needed"
    exit 1
fi

echo "=== Rebuilding container ==="
docker-compose build --no-cache

echo "=== Starting container ==="
docker-compose up -d

echo "=== Waiting for startup ==="
sleep 3

echo "=== Checking logs ==="
docker-compose logs --tail=20

echo ""
echo "=== Done! Test the connection now ==="
echo "If you still see 405 errors, check that you entered username/password in the UI"
