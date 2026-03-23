import io
from datetime import datetime, timezone

from bson import ObjectId
from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from services.document_processor import DocumentProcessingError, analyze_document
from services.pricing_engine import calculate_pricing
from services.queue_predictor import derive_queue_priority, predict_queue_time_minutes
from utils.db import get_db, get_fs

try:
    import fitz
except ImportError:
    fitz = None

try:
    from docx import Document
except ImportError:
    Document = None

main_bp = Blueprint("main", __name__)


def _expand_page_ranges(page_ranges: str, total_pages: int) -> list[int]:
    total_pages = max(1, int(total_pages))
    normalized = (page_ranges or "all").strip().lower()
    if normalized in {"", "all", "*"}:
        return list(range(1, total_pages + 1))

    pages: set[int] = set()
    for chunk in normalized.split(","):
        part = chunk.strip()
        if not part:
            continue
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start = int(start_str)
            end = int(end_str)
            if start > end:
                start, end = end, start
            if start < 1 or end > total_pages:
                raise ValueError("Page range is out of bounds")
            pages.update(range(start, end + 1))
        else:
            page_num = int(part)
            if page_num < 1 or page_num > total_pages:
                raise ValueError("Page number is out of bounds")
            pages.add(page_num)

    if not pages:
        raise ValueError("No valid pages selected")
    return sorted(pages)


def _detect_page_count(file_bytes: bytes, filename: str) -> int:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if extension == "pdf" and fitz is not None:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            page_count = doc.page_count
            doc.close()
            return max(1, page_count)
        if extension == "docx" and Document is not None:
            doc = Document(io.BytesIO(file_bytes))
            text_len = sum(len((p.text or "").strip()) for p in doc.paragraphs)
            return max(1, (text_len // 2600) + 1)
    except Exception:
        pass
    return 1


def _ensure_analysis(db, fs, job: dict) -> dict:
    analytics = db.analytics.find_one({"job_id": job["_id"]})
    if analytics:
        return analytics

    file_obj = fs.get(job["file_id"])
    file_bytes = file_obj.read()
    analysis = analyze_document(file_bytes=file_bytes, filename=job["filename"])

    analytics_doc = {
        "job_id": job["_id"],
        "page_count": analysis["page_count"],
        "color_pages": analysis["color_pages"],
        "bw_pages": analysis["bw_pages"],
        "overall_color_density": analysis["overall_color_density"],
        "total_estimated_print_time_sec": analysis["total_estimated_print_time_sec"],
        "page_metrics": analysis["page_metrics"],
        "created_at": datetime.now(timezone.utc),
        "estimated_wait_minutes": job.get("estimated_wait_minutes", 0),
        "queue_priority": job.get("queue_priority", "normal"),
    }
    analysis_id = db.analytics.insert_one(analytics_doc).inserted_id
    db.jobs.update_one(
        {"_id": job["_id"]},
        {
            "$set": {
                "analysis_id": analysis_id,
                "page_count": analysis["page_count"],
                "selected_page_count": min(max(1, int(job.get("selected_page_count") or 1)), analysis["page_count"]),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    return db.analytics.find_one({"_id": analysis_id})


def _compute_selected_counts(total_pages: int, selected_count: int, color_pages: int) -> tuple[int, int]:
    total_pages = max(1, int(total_pages))
    color_ratio = max(0.0, min(1.0, color_pages / total_pages))
    selected_color_pages = min(selected_count, round(selected_count * color_ratio))
    selected_bw_pages = max(0, selected_count - selected_color_pages)
    return selected_bw_pages, selected_color_pages


@main_bp.route("/")
def index():
    return render_template("index.html")


@main_bp.route("/upload", methods=["POST"])
def upload():
    db = get_db()
    fs = get_fs()

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please choose a file to upload.", "error")
        return redirect(url_for("main.index"))

    extension = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    allowed = {"pdf", "docx", "jpg", "jpeg", "png"}
    if extension not in allowed:
        flash("Unsupported file type. Allowed: PDF, DOCX, JPG, JPEG, PNG.", "error")
        return redirect(url_for("main.index"))

    file_bytes = file.read()
    if not file_bytes:
        flash("Uploaded file is empty.", "error")
        return redirect(url_for("main.index"))

    max_bytes = 20 * 1024 * 1024
    if len(file_bytes) > max_bytes:
        flash("File too large. Max size is 20 MB.", "error")
        return redirect(url_for("main.index"))

    try:
        gridfs_id = fs.put(file_bytes, filename=file.filename, content_type=file.content_type)
        queue_load = db.jobs.count_documents({"status": {"$in": ["payment_pending", "paid", "printing"]}})
        page_count = _detect_page_count(file_bytes=file_bytes, filename=file.filename)
        predicted_wait = predict_queue_time_minutes(
            page_count=page_count,
            color_density=0.0,
            queue_load=queue_load,
            copy_count=1,
        )
        queue_priority = derive_queue_priority(predicted_wait)

        job_doc = {
            "filename": file.filename,
            "file_id": gridfs_id,
            "status": "analyzed",
            "payment_status": "pending",
            "print_status": "pending",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "page_count": page_count,
            "selected_page_count": page_count,
            "page_ranges": "all",
            "copies": 1,
            "mode": "bw",
            "queue_priority": queue_priority,
            "estimated_wait_minutes": predicted_wait,
        }
        job_id = db.jobs.insert_one(job_doc).inserted_id

        session["job_id"] = str(job_id)
        flash("Upload completed. Choose print options.", "success")
        return redirect(url_for("main.options"))
    except DocumentProcessingError as exc:
        flash(f"Document processing failed: {exc}", "error")
        return redirect(url_for("main.index"))
    except Exception:
        flash("Upload failed due to a server error.", "error")
        return redirect(url_for("main.index"))


@main_bp.route("/options", methods=["GET", "POST"])
def options():
    job_id = session.get("job_id")
    if not job_id:
        flash("Session expired. Upload document again.", "error")
        return redirect(url_for("main.index"))

    db = get_db()
    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    fs = get_fs()
    analytics = db.analytics.find_one({"job_id": ObjectId(job_id)})

    if not job:
        flash("Job details not found.", "error")
        return redirect(url_for("main.index"))

    if request.method == "POST":
        copies = request.form.get("copies", "1")
        mode = request.form.get("mode", "bw")
        page_ranges = request.form.get("page_ranges", "all").strip()

        if mode not in {"bw", "color"}:
            flash("Invalid print mode.", "error")
            return redirect(url_for("main.options"))

        try:
            copies = max(1, int(copies))
        except ValueError:
            flash("Copies must be a number.", "error")
            return redirect(url_for("main.options"))

        try:
            selected_pages = _expand_page_ranges(page_ranges=page_ranges, total_pages=job.get("page_count", 1))
        except ValueError as exc:
            flash(f"Invalid page selection: {exc}", "error")
            return redirect(url_for("main.options"))

        if mode == "color":
            try:
                analytics = _ensure_analysis(db=db, fs=fs, job=job)
            except DocumentProcessingError as exc:
                flash(f"Color analysis failed: {exc}", "error")
                return redirect(url_for("main.options"))
            except Exception:
                flash("Unable to run color analysis for this file.", "error")
                return redirect(url_for("main.options"))

        selected_count = len(selected_pages)
        if mode == "color" and analytics:
            selected_bw_pages, selected_color_pages = _compute_selected_counts(
                total_pages=analytics["page_count"],
                selected_count=selected_count,
                color_pages=analytics["color_pages"],
            )
            color_density = analytics["overall_color_density"]
        else:
            selected_color_pages = 0
            selected_bw_pages = selected_count
            color_density = 0.0

        pricing = calculate_pricing(
            page_count=selected_count,
            bw_pages=selected_bw_pages,
            color_pages=selected_color_pages,
            color_density=color_density,
            copies=copies,
            mode=mode,
        )

        queue_load = db.jobs.count_documents({"status": {"$in": ["payment_pending", "paid", "printing"]}})
        predicted_wait = predict_queue_time_minutes(
            page_count=selected_count,
            color_density=color_density,
            queue_load=queue_load,
            copy_count=copies,
        )
        queue_priority = derive_queue_priority(predicted_wait)

        db.jobs.update_one(
            {"_id": ObjectId(job_id)},
            {
                "$set": {
                    "copies": copies,
                    "mode": mode,
                    "page_ranges": page_ranges or "all",
                    "selected_page_count": selected_count,
                    "pricing": pricing.__dict__,
                    "estimated_wait_minutes": predicted_wait,
                    "queue_priority": queue_priority,
                    "status": "payment_pending",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

        if analytics:
            db.analytics.update_one(
                {"job_id": ObjectId(job_id)},
                {
                    "$set": {
                        "estimated_wait_minutes": predicted_wait,
                        "queue_priority": queue_priority,
                    }
                },
            )

        return redirect(url_for("payment.payment"))

    selected_count = int(job.get("selected_page_count", job.get("page_count", 1)))
    mode = job.get("mode", "bw")
    if mode == "color" and analytics:
        selected_bw_pages, selected_color_pages = _compute_selected_counts(
            total_pages=analytics["page_count"],
            selected_count=selected_count,
            color_pages=analytics["color_pages"],
        )
        color_density = analytics["overall_color_density"]
    else:
        selected_color_pages = 0
        selected_bw_pages = selected_count
        color_density = 0.0

    pricing = calculate_pricing(
        page_count=selected_count,
        bw_pages=selected_bw_pages,
        color_pages=selected_color_pages,
        color_density=color_density,
        copies=job.get("copies", 1),
        mode=mode,
    )

    return render_template(
        "options.html",
        job=job,
        analytics=analytics,
        pricing=pricing,
    )


@main_bp.route("/options/analyze-color", methods=["POST"])
def analyze_color():
    job_id = session.get("job_id")
    if not job_id:
        return jsonify({"ok": False, "error": "Session expired. Upload document again."}), 400

    db = get_db()
    fs = get_fs()
    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        return jsonify({"ok": False, "error": "Job not found."}), 404

    try:
        payload = request.get_json(silent=True) or {}
        copies = max(1, int(payload.get("copies", job.get("copies", 1))))
        page_ranges = str(payload.get("page_ranges", job.get("page_ranges", "all"))).strip() or "all"

        analytics = _ensure_analysis(db=db, fs=fs, job=job)
        selected_pages = _expand_page_ranges(page_ranges=page_ranges, total_pages=analytics["page_count"])
        selected_count = len(selected_pages)
        selected_bw_pages, selected_color_pages = _compute_selected_counts(
            total_pages=analytics["page_count"],
            selected_count=selected_count,
            color_pages=analytics["color_pages"],
        )

        pricing = calculate_pricing(
            page_count=selected_count,
            bw_pages=selected_bw_pages,
            color_pages=selected_color_pages,
            color_density=analytics["overall_color_density"],
            copies=copies,
            mode="color",
        )

        queue_load = db.jobs.count_documents({"status": {"$in": ["payment_pending", "paid", "printing"]}})
        predicted_wait = predict_queue_time_minutes(
            page_count=selected_count,
            color_density=analytics["overall_color_density"],
            queue_load=queue_load,
            copy_count=copies,
        )
        queue_priority = derive_queue_priority(predicted_wait)

        db.jobs.update_one(
            {"_id": job["_id"]},
            {
                "$set": {
                    "copies": copies,
                    "mode": "color",
                    "page_ranges": page_ranges,
                    "selected_page_count": selected_count,
                    "pricing": pricing.__dict__,
                    "estimated_wait_minutes": predicted_wait,
                    "queue_priority": queue_priority,
                    "status": "payment_pending",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        db.analytics.update_one(
            {"job_id": job["_id"]},
            {"$set": {"estimated_wait_minutes": predicted_wait, "queue_priority": queue_priority}},
        )

        return jsonify({"ok": True})
    except DocumentProcessingError as exc:
        return jsonify({"ok": False, "error": f"Color analysis failed: {exc}"}), 400
    except ValueError as exc:
        return jsonify({"ok": False, "error": f"Invalid page selection: {exc}"}), 400
    except Exception:
        return jsonify({"ok": False, "error": "Unable to run color analysis."}), 500
