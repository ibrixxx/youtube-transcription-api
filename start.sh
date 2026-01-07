#!/bin/bash
# Start Tor in background
echo "Starting Tor..."
tor &

# Wait for Tor to be ready (wait for port 9050)
echo "Waiting for Tor to be ready..."
timeout 30 bash -c 'until echo > /dev/tcp/127.0.0.1/9050; do sleep 1; done'
if [ $? -eq 0 ]; then
    echo "Tor is ready!"
else
    echo "Tor failed to start or is not listening on 9050. Proceeding anyway (might rely on direct connection)..."
fi

# Start POT (Proof of Origin Token) provider server
# This helps bypass YouTube's "Sign in to confirm you're not a bot" detection
# by generating tokens using Google's BotGuard library
echo "Starting POT Token provider on port 4416..."
bgutil-pot server --port 4416 &
POT_PID=$!

# Wait for POT server to be ready
sleep 3

# Verify POT server is running
if kill -0 $POT_PID 2>/dev/null; then
    echo "POT Token provider is running (PID: $POT_PID)"
else
    echo "Warning: POT Token provider failed to start. yt-dlp will work but may hit bot detection."
fi

# Start the API
echo "Starting API..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}

