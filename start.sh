#!/bin/bash

# Function to wait for a port to be ready with proper verification
wait_for_port() {
    local port=$1
    local service=$2
    local max_attempts=${3:-30}
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if timeout 2 bash -c "echo > /dev/tcp/127.0.0.1/$port" 2>/dev/null; then
            echo "$service is ready on port $port!"
            return 0
        fi
        echo "Waiting for $service on port $port... ($attempt/$max_attempts)"
        sleep 1
        attempt=$((attempt + 1))
    done
    echo "WARNING: $service failed to start on port $port after $max_attempts attempts"
    return 1
}

# Start Tor in background
echo "Starting Tor..."
tor &

# Wait for Tor to be ready with proper port verification
echo "Waiting for Tor to be ready..."
if wait_for_port 9050 "Tor" 30; then
    echo "Tor proxy is available"
else
    echo "Tor failed to start. Proceeding without Tor proxy..."
fi

# Start POT (Proof of Origin Token) provider server
# This helps bypass YouTube's "Sign in to confirm you're not a bot" detection
# by generating tokens using Google's BotGuard library
echo "Starting POT Token provider on port 4416..."
bgutil-pot server --port 4416 &
POT_PID=$!

# Wait for POT server to be ready with proper port verification
# POT needs more time to initialize than just checking if process exists
echo "Waiting for POT Token provider to be ready..."
if wait_for_port 4416 "POT Token provider" 30; then
    echo "POT Token provider is available (PID: $POT_PID)"
else
    echo "WARNING: POT Token provider failed to start. Videos without captions may fail."
    # Check if process is at least running
    if kill -0 $POT_PID 2>/dev/null; then
        echo "POT process is running but port not ready - may still work"
    fi
fi

# Start the API
echo "Starting API..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
