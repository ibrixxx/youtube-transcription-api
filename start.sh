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

# Start the API
echo "Starting API..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}

