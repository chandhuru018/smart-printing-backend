from datetime import datetime, timezone

from flask import Blueprint, flash, redirect, render_template, url_for

from utils.db import get_db, get_fs

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/")
def dashboard():
    db = get_db()

    total_jobs = db.jobs.count_documents({})
    printed_jobs = db.jobs.count_documents({"print_status": "printed"})
    revenue_pipeline = [
        {"$match": {"payment_status": "paid"}},
        {"$group": {"_id": None, "revenue": {"$sum": "$pricing.total_cost"}}},
    ]
    revenue_result = list(db.jobs.aggregate(revenue_pipeline))
    total_revenue = revenue_result[0]["revenue"] if revenue_result else 0

    printer_status = db.printer_status.find_one({"_id": "current"}) or {
        "paper_remaining": 0,
        "ink_remaining_ratio": 0,
        "alerts": [],
        "updated_at": datetime.now(timezone.utc),
    }

    queue_jobs = list(
        db.jobs.find(
            {"status": {"$in": ["payment_pending", "paid", "printing"]}},
            {"filename": 1, "status": 1, "estimated_wait_minutes": 1, "queue_priority": 1, "created_at": 1},
        ).sort("created_at", 1)
    )

    recent_revenue = list(
        db.jobs.find(
            {"payment_status": "paid"},
            {"filename": 1, "pricing.total_cost": 1, "payment_id": 1, "updated_at": 1},
        )
        .sort("updated_at", -1)
        .limit(10)
    )

    open_alerts = list(db.maintenance_alerts.find({"open": True}).sort("created_at", -1).limit(20))

    return render_template(
        "admin/dashboard.html",
        total_jobs=total_jobs,
        printed_jobs=printed_jobs,
        total_revenue=round(total_revenue, 2),
        printer_status=printer_status,
        queue_jobs=queue_jobs,
        recent_revenue=recent_revenue,
        open_alerts=open_alerts,
    )


@admin_bp.route("/clear-data", methods=["POST"])
def clear_data():
    db = get_db()
    fs = get_fs()

    try:
        file_ids = [j.get("file_id") for j in db.jobs.find({}, {"file_id": 1}) if j.get("file_id")]
        for file_id in file_ids:
            try:
                fs.delete(file_id)
            except Exception:
                pass

        db.jobs.delete_many({})
        db.analytics.delete_many({})
        db.maintenance_alerts.delete_many({})
        db.fs.files.delete_many({})
        db.fs.chunks.delete_many({})
        db.printer_status.update_one(
            {"_id": "current"},
            {
                "$set": {
                    "paper_remaining": 0,
                    "ink_remaining_ratio": 0,
                    "alerts": [],
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        flash("All uploaded files and job data were cleared from admin and MongoDB.", "success")
    except Exception:
        flash("Failed to clear data. Please check MongoDB connection and try again.", "error")

    return redirect(url_for("admin.dashboard"))
