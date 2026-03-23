import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Dict

try:
    import razorpay
except ImportError:
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
        webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

        self.config = RazorpayConfig(key_id=key_id, key_secret=key_secret, webhook_secret=webhook_secret)
        self.enabled = bool(key_id and key_secret and webhook_secret and razorpay is not None)
        self.client = razorpay.Client(auth=(key_id, key_secret)) if self.enabled else None

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
