import os
import sys
import time
import logging
import threading

# Add application directory to python path
app_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(app_dir)
sys.path.insert(0, app_dir)

try:
    import webview
except ImportError:
    raise SystemExit(
        "\n"
        "  Desktop dependencies missing!\n"
        "  Run: pip install -r requirements-desktop.txt\n"
    )
import logzero

log = logging.getLogger("desktop_launcher")
logging.basicConfig(level=logging.INFO)

def start_flask():
    try:
        from app import app
        port = int(os.environ.get("PORT", 5050))
        # use_reloader=False is required inside a thread to prevent multiple instances
        app.run(debug=False, host="127.0.0.1", port=port, use_reloader=False)
    except Exception as exc:
        log.error("Flask server encountered an error: %s", exc)

if __name__ == "__main__":
    log.info("Initializing Smart Screener Pro Desktop...")
    
    # 1. Start the Flask application in a background daemon thread
    flask_thread = threading.Thread(target=start_flask, daemon=True, name="flask-backend")
    flask_thread.start()
    
    # 2. Wait for the Flask server to initialize
    time.sleep(2.0)
    
    # 3. Create a dedicated webview window matching screen proportions
    log.info("Opening desktop application window...")
    window = webview.create_window(
        title="Smart Screener Pro — Institutional Quant Terminal",
        url="http://127.0.0.1:5050",
        width=1600,
        height=900,
        min_size=(1024, 768),
        resizable=True
    )
    
    # 4. Start the native desktop loop (this will block until the window is closed)
    webview.start()
    log.info("Smart Screener Pro window closed. Exiting.")
