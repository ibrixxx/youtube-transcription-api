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

# Function to verify Tor connectivity by checking external IP
verify_tor_connectivity() {
    echo "Verifying Tor connectivity..."

    # Try to get IP through Tor proxy
    local tor_ip=$(curl -s --socks5 127.0.0.1:9050 --connect-timeout 10 https://api.ipify.org 2>/dev/null)

    if [ -n "$tor_ip" ]; then
        echo "Tor is working! External IP via Tor: $tor_ip"
        return 0
    else
        echo "WARNING: Could not verify Tor connectivity (external IP check failed)"
        return 1
    fi
}

# Start Tor in background
echo "Starting Tor..."
tor &

# Wait for Tor to be ready with proper port verification
echo "Waiting for Tor to be ready..."
if wait_for_port 9050 "Tor" 30; then
    echo "Tor SOCKS proxy is available on port 9050"

    # Give Tor a moment to establish circuits
    sleep 5

    # Verify actual connectivity (optional - may fail in some environments)
    verify_tor_connectivity || echo "Note: Tor may still work, connectivity check is informational"
else
    echo "Tor failed to start. Proceeding without Tor proxy..."
    echo "Note: YouTube downloads may be blocked without IP rotation"
fi

# Note: POT provider removed - using deno + remote_components for JS challenges instead
# This is simpler and more reliable than running a separate POT server

# Log environment info
echo ""
echo "=== Environment Info ==="
echo "Python version: $(python --version 2>&1)"
echo "Deno version: $(deno --version 2>&1 | head -1)"
echo "FFmpeg version: $(ffmpeg -version 2>&1 | head -1)"
echo "========================"
echo ""

# Start the API
echo "Starting API..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
