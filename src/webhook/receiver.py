"""
Pipeline Paramedic — Webhook Receiver
Receives GitLab pipeline failure webhooks and triggers the agent.
"""

import hashlib
import hmac
import json
import logging
import os

from flask import Flask, abort, jsonify, request

from src.agent.paramedic import PipelineParamedic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

WEBHOOK_SECRET = os.environ.get("GITLAB_WEBHOOK_SECRET", "")


def verify_signature(payload: bytes, signature: str) -> bool:
    """Verify the GitLab webhook X-Gitlab-Token header."""
    if not WEBHOOK_SECRET:
        logger.warning("GITLAB_WEBHOOK_SECRET not set — skipping verification (dev mode)")
        return True
    expected = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "pipeline-paramedic"})


@app.route("/webhook/gitlab", methods=["POST"])
def gitlab_webhook():
    # Verify secret token (GitLab sends it as X-Gitlab-Token, not as a signature)
    token = request.headers.get("X-Gitlab-Token", "")
    if WEBHOOK_SECRET and token != WEBHOOK_SECRET:
        logger.warning("Webhook token mismatch — rejecting request")
        abort(401)

    event_type = request.headers.get("X-Gitlab-Event", "")
    if event_type not in ("Pipeline Hook", "Job Hook"):
        logger.info(f"Ignoring event type: {event_type}")
        return jsonify({"status": "ignored", "reason": f"event type '{event_type}' not handled"})

    payload = request.get_json(force=True)
    if not payload:
        abort(400)

    logger.info(f"Received {event_type}: {json.dumps(payload, indent=2)[:500]}")

    # Only act on failed pipelines
    status = payload.get("object_attributes", {}).get("status") or payload.get("build_status")
    if status != "failed":
        return jsonify({"status": "ignored", "reason": f"pipeline status is '{status}', not 'failed'"})

    # Hand off to the agent (async in production — here synchronous for demo simplicity)
    try:
        paramedic = PipelineParamedic()
        result = paramedic.handle_failure(payload)
        return jsonify({"status": "handled", "result": result})
    except Exception as exc:
        logger.exception("Agent error")
        return jsonify({"status": "error", "message": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
