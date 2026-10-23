import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


def api_exception_handler(exc, context):
    """Consistent error envelope: {"error": {"code", "message", "details"}}. Never leaks internals."""
    response = exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled API error", exc_info=exc)
        return Response(
            {"error": {"code": "server_error", "message": "Something went wrong. Please try again.", "details": None}},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    data = response.data
    if isinstance(data, dict) and "detail" in data and len(data) == 1:
        message, details = str(data["detail"]), None
    else:
        message, details = "Validation failed." if response.status_code == 400 else "Request failed.", data
    code = getattr(getattr(exc, "detail", None), "code", None) or getattr(exc, "default_code", "error")
    response.data = {"error": {"code": str(code), "message": message, "details": details}}
    return response
