import hashlib
import hmac
import logging
import os
import sys
import types
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# pkg_resources compatibility shim for Python 3.12+ / Render Python 3.14
# razorpay uses pkg_resources.get_distribution() to read its own version.
# On modern Python + Render, setuptools may not expose pkg_resources as a
# top-level import. Inject a minimal shim via importlib.metadata so razorpay
# imports cleanly without depending on the environment having setuptools set up.
# ---------------------------------------------------------------------------
if "pkg_resources" not in sys.modules:
    try:
        import pkg_resources  # noqa: F401 (already importable — nothing to do)
    except ImportError:
        import importlib.metadata as _ilm  # noqa: F401

        def _get_dist(name: str):
            try:
                return types.SimpleNamespace(version=_ilm.version(name))
            except Exception:  # noqa: BLE001
                return types.SimpleNamespace(version="unknown")

        _shim = types.ModuleType("pkg_resources")
        _shim.get_distribution = _get_dist
        _shim.DistributionNotFound = Exception
        _shim.require = lambda *a, **kw: None
        sys.modules["pkg_resources"] = _shim
        logger.info("[PaymentService] injected pkg_resources shim (importlib.metadata)")

try:
    import razorpay
except Exception as _razorpay_import_err:  # noqa: BLE001
    logger.warning("razorpay package could not be imported: %s", _razorpay_import_err)
    razorpay = None



class PaymentConfigurationError(Exception):
    pass


class PaymentVerificationError(Exception):
    pass


@dataclass
class RazorpayConfig:
    key_id: str
    key_secret: str
    webhook_secret: str


class PaymentService:
    def __init__(self):
        key_id = os.getenv("RAZORPAY_KEY_ID", "")
        key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")
        # RAZORPAY_WEBHOOK_SECRET is OPTIONAL — only needed to verify Razorpay webhook POSTs.
        # Payment creation and client-side signature verification only need KEY_ID + KEY_SECRET.
        webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

        self.config = RazorpayConfig(key_id=key_id, key_secret=key_secret, webhook_secret=webhook_secret)
        # Only KEY_ID and KEY_SECRET are required to enable live payments.
        self.enabled = bool(key_id and key_secret and razorpay is not None)
        self.client = razorpay.Client(auth=(key_id, key_secret)) if self.enabled else None

        logger.info(
            "[PaymentService] enabled=%s key_id=%s key_secret=%s webhook_secret=%s razorpay_pkg=%s",
            self.enabled,
            "SET" if key_id else "MISSING",
            "SET" if key_secret else "MISSING",
            "SET" if webhook_secret else "MISSING",
            "ok" if razorpay is not None else "import-failed",
        )

    def assert_configured(self):
        if not self.enabled:
            raise PaymentConfigurationError(
                "Missing Razorpay setup. Ensure RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET and razorpay package are configured."
            )

    def create_order(self, amount_rupees: float, receipt: str, notes: Dict) -> Dict:
        self.assert_configured()
        payload = {
            "amount": int(round(amount_rupees * 100)),
            "currency": "INR",
            "receipt": receipt,
            "payment_capture": 1,
            "notes": notes,
        }
        return self.client.order.create(payload)

    def verify_payment_signature(self, razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str):
        self.assert_configured()
        body = f"{razorpay_order_id}|{razorpay_payment_id}"
        expected = hmac.new(
            self.config.key_secret.encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, razorpay_signature):
            raise PaymentVerificationError("Invalid payment signature")

    def verify_webhook_signature(self, payload: bytes, signature: str):
        self.assert_configured()
        digest = hmac.new(
            self.config.webhook_secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(digest, signature):
            raise PaymentVerificationError("Invalid webhook signature")
