import logging
import os
import socket
from flask import Flask
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from gridfs import GridFS
import certifi

from routes.main import main_bp
from routes.payment import payment_bp
from routes.admin import admin_bp
from services.maintenance_monitor import MaintenanceMonitor


def _detect_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def create_app() -> Flask:
    # Load .env from this service directory regardless of process working directory.
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(dotenv_path=env_path)

    app = Flask(__name__)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")
    app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
    app.config["ALLOWED_EXTENSIONS"] = {"pdf", "docx", "jpg", "jpeg", "png"}

    mongodb_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")
    if not mongodb_uri:
        raise RuntimeError("Missing MONGO_URI/MONGODB_URI in .env")

    db_name = os.getenv("MONGO_DB_NAME", "printer")
    use_mock_on_failure = os.getenv("USE_MOCK_DB_ON_FAILURE", "true").lower() == "true"

    try:
        client = MongoClient(
            mongodb_uri,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=10000,
        )
        db = client[db_name]
        client.admin.command("ping")
    except PyMongoError as exc:
        if not use_mock_on_failure:
            raise RuntimeError(
                "Unable to connect to MongoDB. Verify MONGO_URI/MONGODB_URI, Atlas IP access list, and TLS certificates."
            ) from exc

        try:
            import mongomock
            from mongomock.gridfs import enable_gridfs_integration

            enable_gridfs_integration()
            client = mongomock.MongoClient()
            db = client[db_name]
            app.logger.warning("MongoDB unreachable, running with in-memory mock DB: %s", exc)
        except Exception as mock_exc:
            raise RuntimeError(
                "MongoDB connection failed and mock fallback is unavailable. Install dependencies and verify Atlas access."
            ) from mock_exc

    fs = GridFS(db)

    app.extensions["mongo_client"] = client
    app.extensions["mongo_db"] = db
    app.extensions["gridfs"] = fs

    app.register_blueprint(main_bp)
    app.register_blueprint(payment_bp)
    app.register_blueprint(admin_bp)

    monitor = MaintenanceMonitor(db=db)
    monitor.start()
    app.extensions["maintenance_monitor"] = monitor

    @app.teardown_appcontext
    def _shutdown(exception=None):
        _ = exception

    return app


app = create_app()


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    lan_ip = _detect_lan_ip()

    print("=" * 72)
    print("SmartIoTPrinting server starting")
    print(f"Local URL : http://127.0.0.1:{port}")
    print(f"LAN URL   : http://{lan_ip}:{port}")
    print("Use the LAN URL on a phone connected to the same Wi-Fi network.")
    print("=" * 72)

    app.run(host=host, port=port, debug=debug, threaded=True, use_reloader=False)
