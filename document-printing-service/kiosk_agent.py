#!/usr/bin/env python3
"""
Smart IoT Printing - Local Kiosk Agent  (kiosk_agent.py)
=========================================================
Run this script on the college CPU that is PHYSICALLY connected to
the EPSON L3110 (or any default Windows printer).

What it does
------------
1. Serves a browser-based Kiosk UI at  http://localhost:5001
   Students type their 4-digit Release PIN to unlock their printout.
2. Calls the cloud backend (/api/kiosk/release) to validate the PIN.
3. A background thread polls MongoDB for `pin_released` jobs,
   downloads the file from GridFS, and sends it to the printer.

Quick Start
-----------
  1. Double-click `start_kiosk_agent.bat`  (or `python kiosk_agent.py`)
  2. Open http://localhost:5001 in the kiosk browser
  3. Keep this window open — closing it stops printing!

Requirements (install once)
---------------------------
  pip install flask requests pymongo certifi python-dotenv

For full PDF/DOCX/image support (recommended):
  pip install PyMuPDF Pillow python-docx

For silent PDF printing on Windows:
  Install SumatraPDF from https://www.sumatrapdfreader.org/

Environment variables  (set in the .env file next to this script)
------------------------------------------------------------------
  MONGO_URI          = mongodb+srv://...   (from MongoDB Atlas)
  CLOUD_BASE_URL     = https://smart-printing-backend-agnl.onrender.com
  SIMULATE_PRINT     = false               (set true to test without a printer)
  PRINTER_NAME       = EPSON L3110         (leave blank to use Windows default)
  KIOSK_PORT         = 5001
"""

import logging
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# -- Load .env before anything else ------------------------------------------
_here = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=_here / ".env")
except ImportError:
    pass  # dotenv optional; use real environment variables

# ── Config ────────────────────────────────────────────────────────────────────
CLOUD_BASE_URL  = os.getenv("CLOUD_BASE_URL",  "https://smart-printing-backend-agnl.onrender.com").rstrip("/")
MONGO_URI       = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI", "")
MONGO_DB_NAME   = os.getenv("MONGO_DB_NAME", "printer")
KIOSK_PORT      = int(os.getenv("KIOSK_PORT", "5001"))
POLL_SECONDS    = int(os.getenv("PRINT_AGENT_POLL_SECONDS", "3"))
SIMULATE_PRINT  = os.getenv("SIMULATE_PRINT", "false").lower() == "true"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kiosk")

# ── Optional heavy imports (graceful fallback) ────────────────────────────────
try:
    import certifi
    _tls = certifi.where()
except ImportError:
    certifi = None
    _tls = True  # pymongo will use system trust store

try:
    from pymongo import MongoClient, ReturnDocument
    from pymongo.errors import PyMongoError
    from gridfs import GridFS
    HAS_MONGO = True
except ImportError:
    HAS_MONGO = False
    log.warning("pymongo not installed. Print agent disabled. Run: pip install pymongo certifi gridfs")

# pkg_resources shim (needed if razorpay/other libs imported later)
if "pkg_resources" not in sys.modules:
    try:
        import pkg_resources  # noqa: F401
    except ImportError:
        import importlib.metadata as _ilm
        import types as _types
        _shim = _types.ModuleType("pkg_resources")
        _shim.get_distribution = lambda n: _types.SimpleNamespace(  # type: ignore[attr-defined]
            version=_ilm.version(n) if hasattr(_ilm, "version") else "0"
        )
        sys.modules["pkg_resources"] = _shim

# PrintService uses PyMuPDF etc. — add parent dir so relative imports work
sys.path.insert(0, str(_here))
try:
    from services.print_service import PrintService, PrintExecutionError, PrinterOfflineError
    HAS_PRINT_SERVICE = True
except Exception as _ps_err:
    HAS_PRINT_SERVICE = False
    log.warning("PrintService not available (%s). Will use simple os.startfile fallback.", _ps_err)

# ── Flask (required) ──────────────────────────────────────────────────────────
try:
    from flask import Flask, jsonify, render_template_string, request
except ImportError:
    print("\n[ERROR] Flask is not installed.")
    print("  Run:  pip install flask\n")
    sys.exit(1)

try:
    import requests as _requests
except ImportError:
    _requests = None  # type: ignore[assignment]
    log.warning("requests not installed. PIN validation via cloud disabled. Run: pip install requests")

# ═══════════════════════════════════════════════════════════════════════════════
#  DATABASE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _connect_db():
    if not HAS_MONGO:
        raise RuntimeError("pymongo not installed")
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI not set in .env")
    kwargs = {"serverSelectionTimeoutMS": 10_000}
    if certifi:
        kwargs["tlsCAFile"] = _tls
    client = MongoClient(MONGO_URI, **kwargs)
    client.admin.command("ping")
    db = client[MONGO_DB_NAME]
    return client, db, GridFS(db)


def _claim_next_job(db):
    """Atomically grab the next pin_released job so two threads can't double-print."""
    now = datetime.now(timezone.utc)
    return db.jobs.find_one_and_update(
        {"print_status": "pin_released"},
        {
            "$set": {
                "status": "printing",
                "print_status": "printing",
                "agent_started_at": now,
                "updated_at": now,
            },
            "$inc": {"print_attempts": 1},
        },
        sort=[("pin_released_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


def _complete_job(db, job_id, result: dict, printed_pages: int):
    status = "printed" if result.get("status") != "simulated" else "simulated_print"
    db.jobs.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": "completed",
                "print_status": status,
                "printed_pages": printed_pages,
                "printed_at": datetime.now(timezone.utc),
                "print_command": result.get("command", ""),
                "print_log": result.get("stdout", ""),
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


# ═══════════════════════════════════════════════════════════════════════════════
#  PRINT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _do_print(job: dict, file_bytes: bytes) -> dict:
    """Send file_bytes to the physical printer.  Returns a result dict."""
    if SIMULATE_PRINT:
        log.info("[SIMULATE] Would print: %s", job.get("filename"))
        return {"status": "simulated", "command": "simulated-print", "stdout": ""}

    if HAS_PRINT_SERVICE:
        svc = PrintService()
        return svc.print_file_bytes(
            file_bytes=file_bytes,
            filename=job.get("filename", "document.pdf"),
            options={
                "copies":      job.get("copies", 1),
                "mode":        job.get("mode", "bw"),
                "page_ranges": job.get("page_ranges", "all"),
            },
        )

    # ── Fallback: write to temp file and use Windows shell print verb ──
    import subprocess, shutil
    suffix = Path(job.get("filename", "doc.pdf")).suffix or ".pdf"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, file_bytes)
        os.close(fd)
        if os.name == "nt":
            import subprocess
            subprocess.run(
                ["powershell", "-Command",
                 f"Start-Process -FilePath '{tmp}' -Verb Print -Wait"],
                timeout=60, check=True
            )
        elif shutil.which("lp"):
            subprocess.run(["lp", tmp], timeout=60, check=True)
        else:
            raise RuntimeError("No print command found (lp / powershell)")
        return {"status": "queued", "command": "shell-print", "stdout": ""}
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        try:
            time.sleep(5)
            os.remove(tmp)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND PRINT THREAD
# ═══════════════════════════════════════════════════════════════════════════════

# Shared state that the web UI can read
_agent_status: dict = {"state": "starting", "last_job": None, "error": None}


def _print_agent_loop():
    global _agent_status

    if not HAS_MONGO or not MONGO_URI:
        _agent_status = {"state": "disabled", "last_job": None,
                         "error": "MONGO_URI not set — edit .env and restart"}
        log.error("Print agent disabled: MONGO_URI not configured.")
        return

    log.info("Print agent starting (poll every %ds)…", POLL_SECONDS)

    while True:
        try:
            client, db, fs = _connect_db()
            _agent_status["state"] = "ready"
            _agent_status["error"] = None
            log.info("Connected to MongoDB. Watching for pin_released jobs…")

            while True:
                try:
                    job = _claim_next_job(db)
                    if not job:
                        time.sleep(POLL_SECONDS)
                        continue

                    fname = job.get("filename", "unknown")
                    _agent_status["state"] = f"printing: {fname}"
                    log.info("-> Printing: %s  (%s)", fname, job["_id"])

                    file_bytes = fs.get(job["file_id"]).read()
                    result     = _do_print(job, file_bytes)

                    pages   = int(job.get("selected_page_count") or job.get("page_count") or 1)
                    copies  = max(1, int(job.get("copies", 1)))
                    _complete_job(db, job["_id"], result, pages * copies)

                    _agent_status["state"]    = "ready"
                    _agent_status["last_job"] = {
                        "filename": fname,
                        "status": result.get("status", "queued"),
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                    log.info("[OK] Done: %s", fname)

                except Exception as err:
                    if "job" in dir() and job:
                        _fail_job(db, job["_id"], "print_failed", str(err))
                    _agent_status["state"] = "ready"
                    _agent_status["error"] = str(err)
                    log.error("Print failed: %s", err)
                    time.sleep(2)

        except Exception as db_err:
            _agent_status["state"] = "db_error"
            _agent_status["error"] = str(db_err)
            log.error("MongoDB error: %s — retrying in 15 s", db_err)
            time.sleep(15)


# ═══════════════════════════════════════════════════════════════════════════════
#  FLASK WEB UI
# ═══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "kiosk-local-secret-32")

KIOSK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart IoT Printing — Kiosk</title>
<style>
  :root {
    --bg: #0f172a; --surface: #1e293b; --border: #334155;
    --accent: #6366f1; --cyan: #06b6d4; --success: #22c55e;
    --danger: #ef4444; --text: #f1f5f9; --muted: #94a3b8;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif;
    min-height: 100vh; display: flex; flex-direction: column; align-items: center;
    justify-content: center; padding: 1.5rem;
  }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 20px;
    padding: 2.5rem; max-width: 480px; width: 100%;
    box-shadow: 0 25px 60px rgba(0,0,0,0.5);
  }
  .logo { font-size: 1.1rem; color: var(--cyan); font-weight: 700; margin-bottom: 0.25rem; }
  h1 { font-size: 1.6rem; font-weight: 800; margin-bottom: 0.4rem; }
  .sub { color: var(--muted); font-size: 0.92rem; margin-bottom: 2rem; }
  label { display: block; font-size: 0.8rem; text-transform: uppercase;
          letter-spacing: .08em; color: var(--muted); margin-bottom: .5rem; }
  .pin-input {
    width: 100%; font-size: 2.4rem; font-family: 'Courier New', monospace;
    font-weight: 800; letter-spacing: .35em; text-align: center;
    background: var(--bg); border: 2px solid var(--border); border-radius: 12px;
    color: var(--text); padding: .7rem; outline: none; transition: border .2s;
  }
  .pin-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(99,102,241,.2); }
  .btn {
    display: block; width: 100%; margin-top: 1.2rem; padding: .9rem;
    font-size: 1rem; font-weight: 700; border: none; border-radius: 12px;
    cursor: pointer; background: linear-gradient(135deg, var(--accent), var(--cyan));
    color: #fff; transition: opacity .15s, transform .1s;
  }
  .btn:hover { opacity: .9; transform: translateY(-1px); }
  .btn:active { transform: translateY(0); }
  .btn:disabled { opacity: .4; cursor: not-allowed; transform: none; }
  .msg { margin-top: 1rem; padding: .75rem 1rem; border-radius: 10px; font-size: .9rem; }
  .msg.ok  { background: rgba(34,197,94,.12); border: 1px solid rgba(34,197,94,.3); color: #86efac; }
  .msg.err { background: rgba(239,68,68,.12); border: 1px solid rgba(239,68,68,.3); color: #fca5a5; }
  .msg.info{ background: rgba(99,102,241,.12); border: 1px solid rgba(99,102,241,.3); color: #a5b4fc; }

  /* Print confirmation overlay */
  .confirm { display:none; text-align:center; }
  .confirm .big-icon { font-size: 4rem; margin-bottom: .5rem; }
  .confirm h2 { font-size: 1.4rem; margin-bottom: .4rem; color: var(--success); }
  .confirm p { color: var(--muted); font-size: .9rem; margin-bottom: 1.5rem; }
  .job-info {
    background: rgba(6,182,212,.08); border: 1px solid rgba(6,182,212,.25);
    border-radius: 12px; padding: 1rem; margin-bottom: 1.2rem; text-align: left;
  }
  .job-info .row { display:flex; justify-content:space-between; font-size:.88rem;
                   padding:.25rem 0; border-bottom: 1px solid var(--border); }
  .job-info .row:last-child { border-bottom: none; }
  .job-info .key { color: var(--muted); }
  .job-info .val { font-weight: 600; color: var(--cyan); }

  /* Agent status pill */
  .status-bar {
    margin-top: 1.8rem; padding: .5rem .9rem; border-radius: 999px;
    font-size: .75rem; display: inline-flex; align-items: center; gap: .4rem;
    background: rgba(255,255,255,.04); border: 1px solid var(--border);
  }
  .dot { width:8px; height:8px; border-radius:50%; }
  .dot.green { background: var(--success); box-shadow: 0 0 6px var(--success); }
  .dot.yellow { background: #eab308; box-shadow: 0 0 6px #eab308; }
  .dot.red { background: var(--danger); box-shadow: 0 0 6px var(--danger); }

  /* Queue section */
  .queue-section { margin-top: 2rem; }
  .queue-section h3 { font-size: .75rem; text-transform:uppercase; letter-spacing:.08em;
                       color: var(--muted); margin-bottom: .6rem; }
  .queue-row {
    display:flex; align-items:center; gap:.75rem; padding:.6rem .75rem;
    border-radius:10px; margin-bottom:.4rem; font-size:.82rem;
    background: rgba(255,255,255,.03); border: 1px solid var(--border);
  }
  .badge {
    padding:.15rem .5rem; border-radius:6px; font-size:.7rem; font-weight:600;
    flex-shrink:0;
  }
  .badge-wait { background:rgba(234,179,8,.15); color:#fde047; }
  .badge-print{ background:rgba(99,102,241,.2); color:#a5b4fc; }
  .badge-done { background:rgba(34,197,94,.15); color:#86efac; }
  .badge-sim  { background:rgba(148,163,184,.12); color:var(--muted); }

  footer { margin-top:2rem; font-size:.72rem; color: var(--muted); text-align:center; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">🖨 Smart IoT Printing</div>
  <h1>Print Release</h1>
  <p class="sub">Enter the 4-digit code shown on your phone after payment.</p>

  <!-- PIN Entry Panel -->
  <div id="pinPanel">
    <label for="pinBox">Release Code</label>
    <input id="pinBox" class="pin-input" type="text" inputmode="numeric"
           pattern="[0-9]{4}" maxlength="4" placeholder="••••" autocomplete="off">
    <button class="btn" id="releaseBtn" onclick="submitPin()">Release Print Job</button>
    <div id="pinMsg" class="msg" style="display:none"></div>
  </div>

  <!-- Confirmation Panel -->
  <div id="confirmPanel" class="confirm">
    <div class="big-icon">🎉</div>
    <h2>Printing Now!</h2>
    <p>Your document is being sent to the printer.</p>
    <div class="job-info" id="jobInfo"></div>
    <div id="printStatus" class="msg info">⏳ Waiting for printer confirmation…</div>
    <button class="btn" onclick="resetForm()" style="margin-top:1rem; background: var(--surface); border: 1px solid var(--border); color: var(--text);">
      ← New PIN Entry
    </button>
  </div>

  <!-- Agent Status -->
  <div style="text-align:center">
    <div class="status-bar" id="agentStatus">
      <div class="dot yellow" id="agentDot"></div>
      <span id="agentText">Connecting…</span>
    </div>
  </div>

  <!-- Queue -->
  <div class="queue-section">
    <h3>Print Queue</h3>
    <div id="queueList"><div style="color:var(--muted);font-size:.82rem">Loading…</div></div>
  </div>
</div>

<footer>College Kiosk CPU · Smart IoT Printing Project · <span id="clock"></span></footer>

<script>
const CLOUD = "{{ cloud_url }}";
let currentJobId = null;
let pollTimer = null;

// ── Clock ──────────────────────────────────────────────────────────────────
function tick() {
  document.getElementById("clock").textContent =
    new Date().toLocaleTimeString("en-IN", {hour:"2-digit",minute:"2-digit",second:"2-digit"});
}
setInterval(tick, 1000); tick();

// ── PIN Submission ──────────────────────────────────────────────────────────
function submitPin() {
  const pin = document.getElementById("pinBox").value.trim();
  if (pin.length !== 4 || !/^\\d{4}$/.test(pin)) {
    showMsg("pinMsg", "err", "Please enter exactly 4 digits.");
    return;
  }
  document.getElementById("releaseBtn").disabled = true;
  showMsg("pinMsg", "info", "Validating PIN with cloud server…");

  fetch("/release-pin", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({pin})
  })
  .then(r => r.json().catch(() => ({ok: false, error: `Invalid JSON response (Status ${r.status})`})))
  .then(d => {
    if (d.ok) {
      currentJobId = d.job_id;
      showConfirm(d);
    } else {
      showMsg("pinMsg", "err", d.error || "Invalid PIN. Try again.");
      document.getElementById("releaseBtn").disabled = false;
    }
  })
  .catch((err) => {
    showMsg("pinMsg", "err", "Network Error: " + (err.message || "Cannot reach server."));
    document.getElementById("releaseBtn").disabled = false;
  });
}

// ── Confirm Panel ──────────────────────────────────────────────────────────
function showConfirm(job) {
  document.getElementById("pinPanel").style.display = "none";
  const p = document.getElementById("confirmPanel");
  p.style.display = "block";

  const modeLabel = job.mode === "color" ? "🌈 Color" : "⬛ Black & White";
  document.getElementById("jobInfo").innerHTML = `
    <div class="row"><span class="key">File</span><span class="val">${job.filename}</span></div>
    <div class="row"><span class="key">Pages</span><span class="val">${job.pages}</span></div>
    <div class="row"><span class="key">Copies</span><span class="val">${job.copies}</span></div>
    <div class="row"><span class="key">Mode</span><span class="val">${modeLabel}</span></div>
  `;
  pollPrintStatus();
}

function pollPrintStatus() {
  if (!currentJobId) return;
  fetch(`/api/kiosk/job/${currentJobId}`)
    .then(r => r.json())
    .then(d => {
      if (!d.ok) return;
      const s = d.job.print_status;
      const el = document.getElementById("printStatus");
      if (s === "printing") {
        el.className = "msg info"; el.textContent = "🖨  Printing in progress…";
        pollTimer = setTimeout(pollPrintStatus, 2000);
      } else if (s === "printed" || s === "simulated_print") {
        el.className = "msg ok";
        el.textContent = s === "simulated_print"
          ? "✅ Print simulated (no hardware). Job recorded."
          : "✅ Printed successfully! Collect your document.";
      } else if (s === "printer_offline") {
        el.className = "msg err"; el.textContent = "⚠ Printer is offline. Please inform staff.";
      } else if (s === "print_failed") {
        el.className = "msg err"; el.textContent = "❌ Printing failed. Please inform staff.";
      } else {
        pollTimer = setTimeout(pollPrintStatus, 2000);
      }
    })
    .catch(() => { pollTimer = setTimeout(pollPrintStatus, 3000); });
}

function resetForm() {
  clearTimeout(pollTimer);
  currentJobId = null;
  document.getElementById("pinBox").value = "";
  document.getElementById("pinPanel").style.display = "block";
  document.getElementById("confirmPanel").style.display = "none";
  document.getElementById("releaseBtn").disabled = false;
  document.getElementById("pinMsg").style.display = "none";
}

// ── Agent & Queue Polling ──────────────────────────────────────────────────
function refreshQueue() {
  fetch("/agent-status")
    .then(r => r.json())
    .then(d => {
      const dot = document.getElementById("agentDot");
      const txt = document.getElementById("agentText");
      if (d.state === "ready") {
        dot.className = "dot green"; txt.textContent = "Print Agent Ready";
      } else if (d.state.startsWith("printing")) {
        dot.className = "dot yellow"; txt.textContent = "🖨 " + d.state;
      } else if (d.state === "disabled") {
        dot.className = "dot red"; txt.textContent = "Agent Disabled";
      } else {
        dot.className = "dot yellow"; txt.textContent = d.state;
      }
    }).catch(() => {});

  fetch(`/api/kiosk/queue`)
    .then(r => r.json())
    .then(d => {
      const list = document.getElementById("queueList");
      if (!d.ok || !d.jobs.length) {
        list.innerHTML = '<div style="color:var(--muted);font-size:.82rem;padding:.4rem 0">No jobs in queue</div>';
        return;
      }
      list.innerHTML = d.jobs.map(j => {
        const badge = badgeFor(j.print_status);
        const name = j.filename.length > 28 ? j.filename.slice(0,26)+"…" : j.filename;
        return `<div class="queue-row">${badge}<span>${name}</span></div>`;
      }).join("");
    }).catch(() => {});
}

function badgeFor(status) {
  if (status === "awaiting_release") return '<span class="badge badge-wait">Awaiting PIN</span>';
  if (status === "pin_released")     return '<span class="badge badge-wait">PIN-Released</span>';
  if (status === "printing")         return '<span class="badge badge-print">Printing…</span>';
  if (status === "printed")          return '<span class="badge badge-done">Printed ✓</span>';
  if (status === "simulated_print")  return '<span class="badge badge-sim">Simulated</span>';
  return `<span class="badge badge-sim">${status}</span>`;
}

function showMsg(id, type, text) {
  const el = document.getElementById(id);
  el.className = "msg " + type;
  el.textContent = text;
  el.style.display = "block";
}

// Auto-submit on 4 digits
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("pinBox").addEventListener("input", e => {
    if (e.target.value.length === 4) document.getElementById("releaseBtn").focus();
  });
  document.getElementById("pinBox").addEventListener("keydown", e => {
    if (e.key === "Enter") submitPin();
  });
  refreshQueue();
  setInterval(refreshQueue, 5000);
});
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(KIOSK_HTML, cloud_url=CLOUD_BASE_URL)

@app.route("/api/kiosk/queue")
def proxy_queue():
    if not _requests: return jsonify({"ok": False, "jobs": []})
    try:
        r = _requests.get(f"{CLOUD_BASE_URL}/api/kiosk/queue", timeout=5)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/kiosk/job/<job_id>")
def proxy_job(job_id):
    if not _requests: return jsonify({"ok": False})
    try:
        r = _requests.get(f"{CLOUD_BASE_URL}/api/kiosk/job/{job_id}", timeout=5)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/release-pin", methods=["POST"])
def kiosk_release_pin():
    """Proxy the PIN to the cloud backend so the kiosk browser can validate it."""
    data = request.get_json(silent=True) or {}
    pin  = str(data.get("pin", "")).strip()

    if not (_requests):
        # No requests lib — try direct MongoDB validation
        return _release_via_mongo(pin)

    try:
        resp = _requests.post(
            f"{CLOUD_BASE_URL}/api/kiosk/release",
            json={"pin": pin},
            timeout=10,
        )
        try:
            return jsonify(resp.json()), resp.status_code
        except Exception:
            log.error("Cloud PIN validation returned Non-JSON: %s", resp.text)
            return _release_via_mongo(pin)
    except Exception as exc:
        log.error("Cloud PIN validation failed: %s", exc)
        # Fallback: try direct MongoDB
        return _release_via_mongo(pin)


def _release_via_mongo(pin: str):
    """Validate PIN directly in MongoDB (fallback if cloud unreachable)."""
    if not HAS_MONGO or not MONGO_URI:
        return jsonify({"ok": False, "error": "Cannot reach cloud and MongoDB not configured"}), 503
    try:
        _, db, _ = _connect_db()
        job = db.jobs.find_one_and_update(
            {"release_pin": pin, "print_status": "awaiting_release"},
            {"$set": {"print_status": "pin_released",
                      "pin_released_at": datetime.now(timezone.utc),
                      "updated_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )
        if not job:
            return jsonify({"ok": False, "error": "PIN not found or already used"}), 404
        return jsonify({
            "ok": True,
            "job_id": str(job["_id"]),
            "filename": job.get("filename", "document"),
            "pages": job.get("page_count", 1),
            "copies": job.get("copies", 1),
            "mode": job.get("mode", "bw"),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": f"DB error: {exc}"}), 500


@app.route("/agent-status")
def agent_status():
    return jsonify(_agent_status)


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def _banner():
    print("\n" + "=" * 66)
    print("  Smart IoT Printing - Local Kiosk Agent")
    print("=" * 66)
    print(f"  Kiosk UI  : http://localhost:{KIOSK_PORT}")
    print(f"  Cloud     : {CLOUD_BASE_URL}")
    print(f"  MongoDB   : {'CONFIGURED [OK]' if MONGO_URI else 'MISSING [X]  (edit .env)'}")
    print(f"  Simulate  : {SIMULATE_PRINT}")
    print(f"  Printer   : {os.getenv('PRINTER_NAME','(Windows Default)')}")
    print("=" * 66)
    print("  Open the Kiosk UI in your browser and leave this window open.")
    print("  Press CTRL+C to stop.\n")


if __name__ == "__main__":
    _banner()

    # Start background print thread
    t = threading.Thread(target=_print_agent_loop, daemon=True, name="print-agent")
    t.start()

    # Start Flask (use_reloader=False so the background thread isn't duplicated)
    app.run(host="0.0.0.0", port=KIOSK_PORT, debug=False, use_reloader=False)