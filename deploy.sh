#!/bin/bash
# Deploy Louisville Open Data Bot to mac-server (192.168.0.218)
# Run from the project directory: ./deploy.sh

set -e

SERVER="evanray@192.168.0.218"
SSH_KEY="/Users/evanray/Projects/personal/mac-server/mac-server-ssh"
REMOTE_DIR="~/louisville-open-data"

echo "=== Creating remote directory ==="
ssh -i "$SSH_KEY" "$SERVER" "mkdir -p $REMOTE_DIR/data $REMOTE_DIR/static"

echo "=== Syncing project files ==="
rsync -avz --progress -e "ssh -i $SSH_KEY" \
  Dockerfile docker-compose.yml requirements.txt .env \
  analytics_agent.py app.py data_model.py \
  "$SERVER:$REMOTE_DIR/"

rsync -avz --progress -e "ssh -i $SSH_KEY" \
  static/ "$SERVER:$REMOTE_DIR/static/"

echo "=== Syncing data files ==="
rsync -avz --progress -e "ssh -i $SSH_KEY" \
  data/*.csv "$SERVER:$REMOTE_DIR/data/"

echo "=== Building and starting container ==="
ssh -i "$SSH_KEY" "$SERVER" "cd $REMOTE_DIR && docker compose up -d --build"

echo "=== Waiting for startup (45s for data load) ==="
sleep 45

echo "=== Health check ==="
ssh -i "$SSH_KEY" "$SERVER" "curl -s http://localhost:8000/api/health"

echo ""
echo "=== Deploy complete ==="
echo "Access at: http://192.168.0.218:8000"
