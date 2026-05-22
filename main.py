"""Main entrypoint — import the Flask app and run it."""
from src.webhook.receiver import app

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
