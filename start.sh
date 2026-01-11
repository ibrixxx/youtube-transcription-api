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

# Note: POT provider removed - using deno + remote_components for JS challenges instead
# This is simpler and more reliable than running a separate POT server

# Start the API
echo "Starting API..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
