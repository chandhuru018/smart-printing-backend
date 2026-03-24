"""Kiosk API endpoints — consumed by kiosk_agent.py on the college CPU.

Routes:
  GET  /api/kiosk/queue         — current print queue for kiosk display
  POST /api/kiosk/release       — validate PIN → mark job pin_released
  GET  /api/kiosk/job/<job_id>  — single job status for polling
"""

from datetime import datetime, timezone

from bson import ObjectId
from flask import Blueprint, jsonify, request
from pymongo import ReturnDocument

from utils.db import get_db

kiosk_bp = Blueprint("kiosk", __name__, url_prefix="/api/kiosk")


@kiosk_bp.route("/queue")
def queue_status():
    """Return current print queue (jobs waiting for PIN, queued, printing, or recent)."""
    db = get_db()
    jobs = list(
        db.jobs.find(
            {
                "print_status": {
                    "$in": [
                        "awaiting_release",
                        "pin_released",
                        "queued_for_agent",
                        "printing",
                        "printed",
                        "simulated_print",
                    ]
                }
            },
            {
                "filename": 1,
                "print_status": 1,
                "paid_at": 1,
                "printed_at": 1,
                "page_count": 1,
                "copies": 1,
                "mode": 1,
            },
        )
        .sort("paid_at", -1)
        .limit(20)
    )
    for j in jobs:
        j["_id"] = str(j["_id"])
        for ts in ("paid_at", "printed_at", "created_at", "pin_released_at"):
            if j.get(ts):
                j[ts] = j[ts].isoformat()
    return jsonify({"ok": True, "jobs": jobs})


@kiosk_bp.route("/release", methods=["POST"])
def release_print():
    """Validate a 4-digit release PIN and mark the matching job ready to print."""
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin", "")).strip()

    if not (len(pin) == 4 and pin.isdigit()):
        return jsonify({"ok": False, "error": "PIN must be exactly 4 digits"}), 400

    db = get_db()
    job = db.jobs.find_one_and_update(
        {
            "release_pin": pin,
            "print_status": "awaiting_release",
        },
        {
            "$set": {
                "print_status": "pin_released",
                "pin_released_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not job:
        return jsonify({"ok": False, "error": "PIN not found or already used. Check your code and try again."}), 404

    return jsonify(
        {
            "ok": True,
            "job_id": str(job["_id"]),
            "filename": job.get("filename", "document"),
            "pages": job.get("page_count", 1),
            "copies": job.get("copies", 1),
            "mode": job.get("mode", "bw"),
        }
    )


@kiosk_bp.route("/job/<job_id>")
def job_status(job_id: str):
    """Return the current status of a single job (for real-time polling from kiosk UI)."""
    try:
        oid = ObjectId(job_id)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid job ID"}), 400

    db = get_db()
    job = db.jobs.find_one(
        {"_id": oid},
        {"print_status": 1, "status": 1, "filename": 1, "printed_at": 1, "print_error": 1},
    )
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404

    job["_id"] = str(job["_id"])
    if job.get("printed_at"):
        job["printed_at"] = job["printed_at"].isoformat()
    return jsonify({"ok": True, "job": job})
