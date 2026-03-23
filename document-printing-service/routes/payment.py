import os
from datetime import datetime, timezone

from bson import ObjectId
from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from services.payment_service import PaymentConfigurationError, PaymentService, PaymentVerificationError
from services.print_service import PrintExecutionError, PrintService, PrinterOfflineError
from utils.db import get_db, get_fs

payment_bp = Blueprint("payment", __name__)


def _use_local_print_agent() -> bool:
    return os.getenv("PRINT_VIA_AGENT", "false").lower() == "true"


def _enqueue_print_for_agent(db, job_id: ObjectId):
    db.jobs.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": "paid",
                "print_status": "queued_for_agent",
                "print_enqueued_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


def _trigger_print(job: dict, db, fs, payment_id: str):
    printer = PrintService()
    file_obj = fs.get(job["file_id"])
    file_bytes = file_obj.read()

    result = printer.print_file_bytes(
        file_bytes=file_bytes,
        filename=job["filename"],
        options={
            "copies": job.get("copies", 1),
            "mode": job.get("mode", "bw"),
            "page_ranges": job.get("page_ranges", "all"),
        },
    )
    selected_page_count = int(job.get("selected_page_count") or job.get("page_count", 0))
    db.jobs.update_one(
        {"_id": job["_id"]},
        {
            "$set": {
                "status": "completed",
                "print_status": "printed" if result.get("status") != "simulated" else "simulated_print",
                "printed_pages": selected_page_count * max(1, int(job.get("copies", 1))),
                "printed_at": datetime.now(timezone.utc),
                "payment_id": payment_id,
                "print_command": result.get("command"),
                "print_log": result.get("stdout"),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


def _mark_payment_success(db, job_id: ObjectId, payment_id: str):
    db.jobs.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": "paid",
                "payment_status": "paid",
                "payment_id": payment_id,
                "paid_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


@payment_bp.route("/payment", methods=["GET"])
def payment():
    job_id = session.get("job_id")
    if not job_id:
        flash("No active print job found.", "error")
        return redirect(url_for("main.index"))

    db = get_db()
    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        flash("Job not found.", "error")
        return redirect(url_for("main.index"))

    pricing = job.get("pricing")
    if not pricing:
        flash("Select print options before payment.", "error")
        return redirect(url_for("main.options"))

    payment_service = PaymentService()
    payment_error = None
    order = None

    try:
        order = payment_service.create_order(
            amount_rupees=pricing["total_cost"],
            receipt=f"job_{job_id}",
            notes={"job_id": str(job["_id"]), "filename": job.get("filename", "document")},
        )

        db.jobs.update_one(
            {"_id": job["_id"]},
            {
                "$set": {
                    "razorpay_order_id": order.get("id"),
                    "status": "payment_pending",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
    except PaymentConfigurationError as exc:
        payment_error = str(exc)
    except Exception:
        payment_error = "Unable to create Razorpay order. Try again shortly."

    return render_template(
        "payment.html",
        job=job,
        pricing=pricing,
        payment_error=payment_error,
        allow_mock_payment=bool(payment_error),
        razorpay_key_id=payment_service.config.key_id,
        razorpay_order=order,
    )


@payment_bp.route("/payment/verify", methods=["POST"])
def verify_payment():
    payload = request.get_json(silent=True) or {}

    job_id = payload.get("job_id") or session.get("job_id")
    order_id = payload.get("razorpay_order_id")
    payment_id = payload.get("razorpay_payment_id")
    signature = payload.get("razorpay_signature")

    if not all([job_id, order_id, payment_id, signature]):
        return jsonify({"ok": False, "error": "Missing payment fields"}), 400

    db = get_db()
    fs = get_fs()
    payment_service = PaymentService()

    try:
        payment_service.verify_payment_signature(order_id, payment_id, signature)
    except (PaymentConfigurationError, PaymentVerificationError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404

    _mark_payment_success(db=db, job_id=job["_id"], payment_id=payment_id)

    if _use_local_print_agent():
        _enqueue_print_for_agent(db=db, job_id=job["_id"])
        return jsonify({"ok": True, "redirect_url": url_for("payment.success")})

    try:
        db.jobs.update_one({"_id": job["_id"]}, {"$set": {"status": "printing", "print_status": "printing"}})
        _trigger_print(job={**job, "payment_id": payment_id}, db=db, fs=fs, payment_id=payment_id)
        return jsonify({"ok": True, "redirect_url": url_for("payment.success")})
    except PrinterOfflineError as exc:
        db.jobs.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": "paid", "print_status": "printer_offline", "print_error": str(exc)}},
        )
        return jsonify({"ok": False, "error": "Printer offline. Payment captured; admin notified."}), 503
    except (PrintExecutionError, Exception) as exc:
        db.jobs.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": "paid", "print_status": "print_failed", "print_error": str(exc)}},
        )
        return jsonify({"ok": False, "error": "Payment captured, but printing failed."}), 500


@payment_bp.route("/payment/webhook", methods=["POST"])
def razorpay_webhook():
    payload = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")
    payment_service = PaymentService()

    try:
        payment_service.verify_webhook_signature(payload, signature)
    except (PaymentConfigurationError, PaymentVerificationError):
        return jsonify({"ok": False}), 400

    event = request.get_json(silent=True) or {}
    event_name = event.get("event", "")

    if event_name == "payment.captured":
        entity = event.get("payload", {}).get("payment", {}).get("entity", {})
        payment_id = entity.get("id")
        order_id = entity.get("order_id")

        if payment_id and order_id:
            db = get_db()
            fs = get_fs()
            job = db.jobs.find_one({"razorpay_order_id": order_id})
            if job and job.get("payment_status") != "paid":
                _mark_payment_success(db=db, job_id=job["_id"], payment_id=payment_id)
                if _use_local_print_agent():
                    _enqueue_print_for_agent(db=db, job_id=job["_id"])
                    return jsonify({"ok": True})
                try:
                    db.jobs.update_one({"_id": job["_id"]}, {"$set": {"status": "printing", "print_status": "printing"}})
                    _trigger_print(job={**job, "payment_id": payment_id}, db=db, fs=fs, payment_id=payment_id)
                except Exception as exc:
                    db.jobs.update_one(
                        {"_id": job["_id"]},
                        {"$set": {"print_status": "print_failed", "print_error": str(exc)}},
                    )

    return jsonify({"ok": True})


@payment_bp.route("/payment/mock-success", methods=["POST"])
def mock_success():
    job_id = session.get("job_id")
    if not job_id:
        flash("No active print job found.", "error")
        return redirect(url_for("main.index"))

    db = get_db()
    fs = get_fs()
    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        flash("Job not found.", "error")
        return redirect(url_for("main.index"))

    payment_id = f"mockpay_{job_id}"
    _mark_payment_success(db=db, job_id=job["_id"], payment_id=payment_id)
    if _use_local_print_agent():
        _enqueue_print_for_agent(db=db, job_id=job["_id"])
        return redirect(url_for("payment.success"))

    db.jobs.update_one({"_id": job["_id"]}, {"$set": {"status": "printing", "print_status": "printing"}})
    _trigger_print(job={**job, "payment_id": payment_id}, db=db, fs=fs, payment_id=payment_id)
    return redirect(url_for("payment.success"))


@payment_bp.route("/success")
def success():
    return render_template("success.html")
