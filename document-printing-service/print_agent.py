import os
import time
from datetime import datetime, timezone

import certifi
from gridfs import GridFS
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError

from services.print_service import PrintExecutionError, PrintService, PrinterOfflineError


def _connect_db():
    mongodb_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")
    if not mongodb_uri:
        raise RuntimeError("Missing MONGO_URI/MONGODB_URI in environment.")

    db_name = os.getenv("MONGO_DB_NAME", "printer")
    client = MongoClient(
        mongodb_uri,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=10000,
    )
    db = client[db_name]
    client.admin.command("ping")
    return client, db, GridFS(db)


def _claim_next_job(db):
    now = datetime.now(timezone.utc)
    return db.jobs.find_one_and_update(
        {
            "payment_status": "paid",
            "print_status": {"$in": ["pin_released", "queued_for_agent", "pending"]},
            "status": {"$in": ["paid", "printing", "payment_pending"]},
        },
        {
            "$set": {
                "status": "printing",
                "print_status": "printing",
                "agent_started_at": now,
                "updated_at": now,
            },
            "$inc": {"print_attempts": 1},
        },
        sort=[("pin_released_at", 1), ("paid_at", 1), ("updated_at", 1)],
        return_document=ReturnDocument.AFTER,
    )



def _complete_job(db, job_id, payment_id: str, print_result: dict, printed_pages: int):
    db.jobs.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": "completed",
                "print_status": "printed" if print_result.get("status") != "simulated" else "simulated_print",
                "printed_pages": printed_pages,
                "printed_at": datetime.now(timezone.utc),
                "payment_id": payment_id,
                "print_command": print_result.get("command"),
                "print_log": print_result.get("stdout"),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


def _fail_job(db, job_id, status: str, error: str):
    db.jobs.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": "paid",
                "print_status": status,
                "print_error": error,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


def run_agent():
    poll_seconds = max(1, int(os.getenv("PRINT_AGENT_POLL_SECONDS", "3")))
    print("Local Print Agent starting...")
    print(f"Polling interval: {poll_seconds}s")
    print("Press CTRL+C to stop.")

    client, db, fs = _connect_db()
    printer = PrintService()

    try:
        while True:
            job = _claim_next_job(db)
            if not job:
                time.sleep(poll_seconds)
                continue

            try:
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
                printed_pages = selected_page_count * max(1, int(job.get("copies", 1)))
                _complete_job(
                    db=db,
                    job_id=job["_id"],
                    payment_id=job.get("payment_id", ""),
                    print_result=result,
                    printed_pages=printed_pages,
                )
                print(f"[OK] Printed: {job.get('filename')} ({job['_id']})")
            except PrinterOfflineError as exc:
                _fail_job(db=db, job_id=job["_id"], status="printer_offline", error=str(exc))
                print(f"[OFFLINE] {job.get('filename')} ({job['_id']}): {exc}")
            except (PrintExecutionError, Exception) as exc:
                _fail_job(db=db, job_id=job["_id"], status="print_failed", error=str(exc))
                print(f"[FAILED] {job.get('filename')} ({job['_id']}): {exc}")
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        run_agent()
    except KeyboardInterrupt:
        print("\nLocal Print Agent stopped.")
    except PyMongoError as exc:
        print(f"MongoDB connection failed: {exc}")
    except Exception as exc:
        print(f"Agent failed: {exc}")
