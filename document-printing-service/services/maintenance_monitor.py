import os
from datetime import datetime, timezone
from pymongo.errors import PyMongoError

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:
    BackgroundScheduler = None


class MaintenanceMonitor:
    def __init__(self, db):
        self.db = db
        self.scheduler = BackgroundScheduler(timezone="UTC") if BackgroundScheduler else None
        self.paper_capacity = int(os.getenv("PAPER_CAPACITY", "1000"))
        self.paper_threshold = int(os.getenv("PAPER_LOW_THRESHOLD", "100"))
        self.ink_threshold = float(os.getenv("INK_LOW_THRESHOLD", "0.2"))

    def start(self):
        if self.scheduler is None:
            self._run_cycle()
            return

        if self.scheduler.running:
            return

        if os.getenv("WERKZEUG_RUN_MAIN") == "false":
            return

        self.scheduler.add_job(self._run_cycle, "interval", minutes=5, id="maintenance_cycle", replace_existing=True)
        self.scheduler.start()
        self._run_cycle()

    def _run_cycle(self):
        try:
            printed_jobs = list(self.db.jobs.find({"print_status": "printed"}))
        except PyMongoError:
            return
        total_printed_pages = sum(j.get("printed_pages", 0) for j in printed_jobs)

        try:
            analytics = list(self.db.analytics.find({}, {"overall_color_density": 1, "page_count": 1}))
        except PyMongoError:
            return
        density_sum = sum(a.get("overall_color_density", 0.0) * a.get("page_count", 0) for a in analytics)
        total_pages_analyzed = max(1, sum(a.get("page_count", 0) for a in analytics))
        avg_density = density_sum / total_pages_analyzed

        paper_remaining = max(0, self.paper_capacity - total_printed_pages)
        ink_remaining_ratio = max(0.0, 1.0 - min(1.0, avg_density * (total_printed_pages / max(1, self.paper_capacity))))

        alerts = []
        if paper_remaining <= self.paper_threshold:
            alerts.append(f"Paper low: {paper_remaining} sheets remaining")
        if ink_remaining_ratio <= self.ink_threshold:
            alerts.append(f"Ink low: {round(ink_remaining_ratio * 100, 1)}% remaining")

        status_payload = {
            "_id": "current",
            "updated_at": datetime.now(timezone.utc),
            "total_printed_pages": total_printed_pages,
            "paper_capacity": self.paper_capacity,
            "paper_remaining": paper_remaining,
            "avg_color_density": round(avg_density, 4),
            "ink_remaining_ratio": round(ink_remaining_ratio, 4),
            "alerts": alerts,
            "admin_email": os.getenv("ADMIN_EMAIL", ""),
        }
        self.db.printer_status.update_one({"_id": "current"}, {"$set": status_payload}, upsert=True)

        if alerts:
            for alert in alerts:
                self.db.maintenance_alerts.update_one(
                    {"message": alert, "open": True},
                    {
                        "$setOnInsert": {
                            "message": alert,
                            "created_at": datetime.now(timezone.utc),
                            "open": True,
                        }
                    },
                    upsert=True,
                )
