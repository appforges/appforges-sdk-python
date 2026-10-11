"""Typed API failures, including both gateway and google.rpc.Status envelopes."""

from __future__ import annotations

import math
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


class AppForgesError(Exception):
    pass


class APIError(AppForgesError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        reason: str = "",
        domain: str = "",
        metadata: dict[str, Any] | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.reason = reason
        self.domain = domain
        self.metadata = metadata or {}
        self.request_id = request_id
        self.trace_id = trace_id
        self.retry_after = retry_after


class RateLimitExceededError(APIError):
    pass


class ResponseValidationError(AppForgesError):
    def __init__(self, *, request_id: str | None, trace_id: str | None) -> None:
        super().__init__("API response does not match the SDK response type")
        self.request_id = request_id
        self.trace_id = trace_id


ERROR_TYPES: dict[str, type[APIError]] = {"RATE_LIMIT_EXCEEDED": RateLimitExceededError}


def register_error(reason: str, error_type: type[APIError]) -> None:
    ERROR_TYPES[reason] = error_type


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    if not value or not value.strip():
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            return max(
                0.0,
                parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now),
            )
        except (ValueError, TypeError, OverflowError):
            return None


def error_from_response(response: httpx.Response) -> APIError:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    status = payload.get("error", payload)
    if not isinstance(status, dict):
        status = {}
    details = status.get("details", {})
    info: dict[str, Any] = {}
    if isinstance(details, dict):
        info = details
    elif isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and (
                "ErrorInfo" in str(item.get("@type", "")) or "reason" in item
            ):
                info = item
                break
    reason = info.get("reason", status.get("reason", ""))
    if not isinstance(reason, str):
        reason = ""
    domain = info.get("domain", "")
    metadata = info.get("metadata", {})
    message = status.get("message", f"API request failed with HTTP {response.status_code}")
    cls = ERROR_TYPES.get(reason, APIError)
    return cls(
        str(message),
        http_status=response.status_code,
        reason=reason,
        domain=domain if isinstance(domain, str) else "",
        metadata=metadata if isinstance(metadata, dict) else {},
        request_id=response.headers.get("x-request-id"),
        trace_id=response.headers.get("x-trace-id"),
        retry_after=parse_retry_after(response.headers.get("retry-after")),
    )
