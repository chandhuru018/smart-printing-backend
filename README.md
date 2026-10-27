# 🖨️ Smart IoT Printing Kiosk

A self-hosted, PIN-secured document printing kiosk system built with **Flask** and **MongoDB Atlas**. Users upload documents and pay via **Razorpay** on their mobile device, then collect their printout from the kiosk by entering a one-time PIN.

---

## 🏗️ Architecture

```
Mobile Browser ──► Cloud Flask Server (Render/Railway)
                         │
                         │  WebSocket / REST polling
                         ▼
                   Local Kiosk Agent (Windows PC)
                         │
                         ▼
                   EPSON L3110 Printer
```

| Component | Description |
|---|---|
| `app.py` | Flask cloud backend — handles uploads, payments, PIN generation |
| `kiosk_agent.py` | Local Windows agent — polls for jobs and silently prints |
| `print_agent.py` | Lightweight print-only agent |

---

## ✅ Prerequisites

- Python **3.10+**
- A **MongoDB Atlas** account (free tier works)
- A **Razorpay** account (test mode is fine to start)
- Windows PC with EPSON L3110 (or any printer) for the kiosk agent

---

## 🚀 Setup & Run

### 1. Clone the repository

```bash
git clone https://github.com/chandhuru018/smart-printing-backend.git
cd smart-printing-backend/document-printing-service
```

### 2. Create a virtual environment & install dependencies

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in your real values:

```env
MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/printer
MONGODB_URI=<same as above>
FLASK_SECRET_KEY=<a long random string>

RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxx
RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
RAZORPAY_WEBHOOK_SECRET=<your webhook secret>

ADMIN_EMAIL=you@example.com
PRINTER_NAME=EPSON L3110 Series
SIMULATE_PRINT=false
MAX_UPLOAD_MB=20
PAPER_CAPACITY=1000
PAPER_LOW_THRESHOLD=100
INK_LOW_THRESHOLD=0.2
MONGO_DB_NAME=printer
USE_MOCK_DB_ON_FAILURE=true
```

### 4. Start the Flask server

```bash
# Option A — using the batch script (Windows)
start_server.bat

# Option B — manually
venv\Scripts\python.exe app.py
```

The server will print:

```
SmartIoTPrinting server starting
Local URL : http://127.0.0.1:5000
LAN URL   : http://192.168.x.x:5000
```

### 5. Start the Kiosk Agent (local PC)

```bash
start_kiosk_agent.bat
```

---

## 📁 Project Structure

```
document-printing-service/
├── app.py                  # Flask application entry point
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── routes/
│   ├── main.py             # File upload & UI routes
│   ├── payment.py          # Razorpay payment routes
│   ├── admin.py            # Admin dashboard
│   └── kiosk.py            # Kiosk agent API endpoints
├── services/
│   └── maintenance_monitor.py
├── utils/                  # Shared helpers
├── templates/              # HTML templates (Jinja2)
├── static/                 # CSS, JS, images
├── kiosk_agent.py          # Local kiosk Windows agent
├── print_agent.py          # Lightweight print agent
├── start_server.bat        # Quick-start for Flask server
├── start_kiosk_agent.bat   # Quick-start for kiosk agent
└── start_all.bat           # Start everything at once
```

---

## 🔑 Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `MONGO_URI` / `MONGODB_URI` | ✅ | MongoDB Atlas connection string |
| `FLASK_SECRET_KEY` | ✅ | Secret key for Flask sessions |
| `RAZORPAY_KEY_ID` | ✅ | Razorpay API key ID |
| `RAZORPAY_KEY_SECRET` | ✅ | Razorpay API secret |
| `RAZORPAY_WEBHOOK_SECRET` | ✅ | Razorpay webhook verification secret |
| `ADMIN_EMAIL` | ✅ | Email for admin dashboard alerts |
| `PRINTER_NAME` | ✅ | Exact Windows printer name |
| `SIMULATE_PRINT` | ❌ | `true` to skip real printing (default: `false`) |
| `MAX_UPLOAD_MB` | ❌ | Max file upload size (default: `20`) |
| `PAPER_CAPACITY` | ❌ | Total paper capacity (default: `1000`) |
| `USE_MOCK_DB_ON_FAILURE` | ❌ | Use in-memory DB if MongoDB is unreachable (default: `true`) |

---

## ☁️ Deploy to Render

A `render.yaml` is included for one-click deployment to [Render](https://render.com).

1. Push the repo to GitHub
2. Connect your GitHub repo in the Render dashboard
3. Set the environment variables under **Environment → Secret Files** (never commit `.env`)
4. Deploy!

---

## 🔒 Security Notes

- **Never commit your `.env` file.** It is listed in `.gitignore`.
- Rotate your MongoDB password and Razorpay keys if you suspect they were exposed.
- Use Razorpay **live keys** only after thorough testing in test mode.

---

## 📄 License

MIT — feel free to use, modify, and distribute.
