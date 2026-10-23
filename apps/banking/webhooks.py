"""Plaid webhook receiver. Requests are verified with Plaid's JWT and a SHA-256 body comparison."""

import hashlib
import json
import logging

import jwt
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.core.services import notify

from .models import PlaidItem
from .services import plaid_client as plaid
from .tasks import refresh_item_status_task, sync_item_task

logger = logging.getLogger(__name__)

SYNC_CODES = {"SYNC_UPDATES_AVAILABLE", "INITIAL_UPDATE", "HISTORICAL_UPDATE", "DEFAULT_UPDATE", "TRANSACTIONS_REMOVED"}
REAUTH_CODES = {"PENDING_EXPIRATION", "PENDING_DISCONNECT", "USER_PERMISSION_REVOKED"}


def verify_plaid_request(raw_body: bytes, token: str | None) -> bool:
    if not token:
        return False
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "ES256" or not header.get("kid"):
            return False
        key = plaid.webhook_verification_key(header["kid"])["key"]
        public_key = jwt.PyJWK(key).key
        payload = jwt.decode(token, public_key, algorithms=["ES256"], options={"verify_aud": False})
        import time

        if time.time() - payload.get("iat", 0) > 300:  # maxTokenAge: 5 min
            return False
        return payload.get("request_body_sha256") == hashlib.sha256(raw_body).hexdigest()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Plaid webhook verification failed: %s", exc)
        return False


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ScopedRateThrottle])
def plaid_webhook(request):
    raw = request.body
    if not verify_plaid_request(raw, request.headers.get("plaid-verification")):
        return Response("Verification failed", status=401)
    try:
        event = json.loads(raw)
    except ValueError:
        return Response("Invalid payload", status=400)

    item = PlaidItem.objects.filter(item_id=event.get("item_id")).select_related("user").first()
    if not item:
        return Response("ok")

    code, kind = event.get("webhook_code"), event.get("webhook_type")
    try:
        if kind == "TRANSACTIONS" and code in SYNC_CODES:
            sync_item_task.delay(str(item.id))
        elif kind == "ITEM":
            if code == "ERROR":
                err = (event.get("error") or {}).get("error_code")
                item.connection_status = "requires_reauth" if err == "ITEM_LOGIN_REQUIRED" else "error"
                item.error_code, item.error_message = err, plaid.friendly_plaid_error(err)
                item.save(update_fields=["connection_status", "error_code", "error_message", "updated_at"])
                notify(item.user, "Bank connection requires attention", item.error_message, "error")
            elif code in REAUTH_CODES:
                item.connection_status, item.error_code = "requires_reauth", code
                item.error_message = plaid.friendly_plaid_error(code)
                item.save(update_fields=["connection_status", "error_code", "error_message", "updated_at"])
                notify(item.user, "Reconnect your bank", item.error_message, "warning")
            else:
                refresh_item_status_task.delay(str(item.id))
    except Exception:  # noqa: BLE001 - return 200 so Plaid does not hot-loop retries
        logger.exception("Webhook processing error")
    return Response("ok")


plaid_webhook.cls.throttle_scope = "webhook"
