#!/bin/bash
cd "$(dirname "$0")"
echo "G One | Nifty 250 Screener v4 — High Conviction Edition"
echo "Starting on http://localhost:5000"
echo ""

# Use gunicorn in production, fallback to Flask dev server
if command -v gunicorn &>/dev/null; then
    gunicorn -w 2 -b 0.0.0.0:5000 --timeout 120 app:app
else
    python3 app.py
fi
