"""Shared HTTP behavior for generated sync and async resource wrappers."""

from __future__ import annotations

import asyncio
import inspect
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from typing import Any, Self

import httpx
from pydantic import TypeAdapter, ValidationError

from .auth import AsyncAuthProvider, AuthProvider
from .errors import ResponseValidationError, error_from_response, parse_retry_after
from .models import NOT_GIVEN, NotGiven, wire_value
from .streaming import SSEDecoder


def _prepare(
    method: str,
    path: str,
    base_url: str,
    query: Mapping[str, Any] | None,
    body: Any,
    headers: Mapping[str, str] | None,
    idempotent: bool,
    idempotency_key: str | None,
) -> tuple[str, dict[str, Any], dict[str, str], bool]:
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise ValueError("request path must be an absolute API path without query or fragment")
    values = {k.lower(): v for k, v in (headers or {}).items()}
    values.setdefault("accept", "application/json")
    if idempotency_key is not None:
        values["x-idempotency-key"] = idempotency_key
    elif idempotent and method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        values.setdefault("x-idempotency-key", str(uuid.uuid4()))
    safe = method.upper() in {"GET", "HEAD", "OPTIONS"} or bool(values.get("x-idempotency-key"))
    params = {
        k: wire_value(v)
        for k, v in (query or {}).items()
        if v is not None and not isinstance(v, NotGiven)
    }
    kwargs: dict[str, Any] = {"params": params}
    if not isinstance(body, NotGiven):
        kwargs["json"] = wire_value(body)
    return base_url.rstrip("/") + path, kwargs, values, safe


def _delay(attempt: int, response: httpx.Response | None) -> float:
    advised = parse_retry_after(response.headers.get("retry-after")) if response else None
    if advised is not None and advised > 0:
        return advised
    return random.uniform(0, min(30.0, 0.2 * 2**attempt))


def _retryable(response: httpx.Response) -> bool:
    return response.status_code == 429 or 500 <= response.status_code <= 599


def _decode(response: httpx.Response, response_type: Any) -> Any:
    if response.status_code >= 400:
        raise error_from_response(response)
    if response.status_code == 204 or not response.content:
        return None
    try:
        return TypeAdapter(response_type).validate_json(response.content, strict=True)
    except (ValidationError, ValueError):
        raise ResponseValidationError(
            request_id=response.headers.get("x-request-id"),
            trace_id=response.headers.get("x-trace-id"),
        ) from None


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


class BaseClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.appforges.com",
        auth: AuthProvider | None = None,
        max_retries: int = 2,
        timeout: float = 60.0,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0 or timeout <= 0:
            raise ValueError("max_retries must be nonnegative and timeout positive")
        self.base_url = base_url
        self.auth = auth
        self.max_retries = max_retries
        self.timeout = timeout
        self._owns_client = http_client is None
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout)
        self._sleep = sleep

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def _send(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Any = NOT_GIVEN,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        url, kwargs, values, safe = _prepare(
            method, path, self.base_url, query, body, headers, idempotent, idempotency_key
        )
        for attempt in range(self.max_retries + 1):
            if self.auth is not None:
                self.auth.apply(values)
            try:
                req = self._http.build_request(
                    method, url, headers=values, timeout=self.timeout, **kwargs
                )
                response = self._http.send(req, stream=stream)
            except (httpx.NetworkError, httpx.RemoteProtocolError):
                if not safe or attempt == self.max_retries:
                    raise
                self._sleep(_delay(attempt, None))
                continue
            if safe and _retryable(response) and attempt < self.max_retries:
                delay = _delay(attempt, response)
                response.close()
                self._sleep(delay)
                continue
            return response
        raise AssertionError("unreachable retry state")

    def request(
        self,
        method: str,
        path: str,
        *,
        response_type: Any = Any,
        query: Mapping[str, Any] | None = None,
        body: Any = NOT_GIVEN,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
    ) -> Any:
        response = self._send(
            method,
            path,
            query=query,
            body=body,
            headers=headers,
            idempotent=idempotent,
            idempotency_key=idempotency_key,
        )
        try:
            return _decode(response, response_type)
        finally:
            response.close()

    def streaming(
        self,
        method: str,
        path: str,
        *,
        response_type: Any = Any,
        query: Mapping[str, Any] | None = None,
        body: Any = NOT_GIVEN,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
    ) -> Iterator[Any]:
        response = self._send(
            method,
            path,
            query=query,
            body=body,
            headers={**(headers or {}), "accept": "text/event-stream"},
            idempotent=idempotent,
            idempotency_key=idempotency_key,
            stream=True,
        )
        try:
            if response.status_code >= 400:
                response.read()
                raise error_from_response(response)
            decoder = SSEDecoder()
            for line in response.iter_lines():
                for data in decoder.line(line):
                    if data == "[DONE]":
                        return
                    yield TypeAdapter(response_type).validate_json(data, strict=True)
            for data in decoder.finish():
                if data != "[DONE]":
                    yield TypeAdapter(response_type).validate_json(data, strict=True)
        finally:
            response.close()

    def paginate(
        self,
        method: str,
        path: str,
        *,
        response_type: Any,
        items_field: str,
        token_field: str,
        token_param: str,
        page_size_param: str | None = None,
        max_pages: int | None = None,
        query: Mapping[str, Any] | None = None,
        body: Any = NOT_GIVEN,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
    ) -> Iterator[Any]:
        del page_size_param
        params = dict(query or {})
        seen: set[str] = set()
        if params.get(token_param):
            seen.add(str(params[token_param]))
        count = 0
        if max_pages is not None and max_pages < 0:
            raise ValueError("max_pages must be nonnegative")
        while max_pages is None or count < max_pages:
            page = self.request(
                method,
                path,
                response_type=response_type,
                query=params,
                body=body,
                headers=headers,
                idempotent=idempotent,
                idempotency_key=idempotency_key,
            )
            yield from _field(page, items_field) or []
            count += 1
            token = _field(page, token_field)
            if not token:
                return
            if not isinstance(token, str) or token in seen:
                raise ValueError("pagination returned a repeated or invalid cursor")
            seen.add(token)
            params[token_param] = token


class AsyncBaseClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.appforges.com",
        auth: AuthProvider | AsyncAuthProvider | None = None,
        max_retries: int = 2,
        timeout: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_retries < 0 or timeout <= 0:
            raise ValueError("max_retries must be nonnegative and timeout positive")
        self.base_url = base_url
        self.auth = auth
        self.max_retries = max_retries
        self.timeout = timeout
        self._owns_client = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient(timeout=timeout)
        self._sleep = sleep

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Any = NOT_GIVEN,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        url, kwargs, values, safe = _prepare(
            method, path, self.base_url, query, body, headers, idempotent, idempotency_key
        )
        for attempt in range(self.max_retries + 1):
            if self.auth is not None:
                result = self.auth.apply(values)
                if inspect.isawaitable(result):
                    await result
            try:
                req = self._http.build_request(
                    method, url, headers=values, timeout=self.timeout, **kwargs
                )
                response = await self._http.send(req, stream=stream)
            except (httpx.NetworkError, httpx.RemoteProtocolError):
                if not safe or attempt == self.max_retries:
                    raise
                await self._sleep(_delay(attempt, None))
                continue
            if safe and _retryable(response) and attempt < self.max_retries:
                delay = _delay(attempt, response)
                await response.aclose()
                await self._sleep(delay)
                continue
            return response
        raise AssertionError("unreachable retry state")

    async def request(
        self,
        method: str,
        path: str,
        *,
        response_type: Any = Any,
        query: Mapping[str, Any] | None = None,
        body: Any = NOT_GIVEN,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
    ) -> Any:
        response = await self._send(
            method,
            path,
            query=query,
            body=body,
            headers=headers,
            idempotent=idempotent,
            idempotency_key=idempotency_key,
        )
        try:
            return _decode(response, response_type)
        finally:
            await response.aclose()

    async def streaming(
        self,
        method: str,
        path: str,
        *,
        response_type: Any = Any,
        query: Mapping[str, Any] | None = None,
        body: Any = NOT_GIVEN,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
    ) -> AsyncIterator[Any]:
        response = await self._send(
            method,
            path,
            query=query,
            body=body,
            headers={**(headers or {}), "accept": "text/event-stream"},
            idempotent=idempotent,
            idempotency_key=idempotency_key,
            stream=True,
        )
        try:
            if response.status_code >= 400:
                await response.aread()
                raise error_from_response(response)
            decoder = SSEDecoder()
            async for line in response.aiter_lines():
                for data in decoder.line(line):
                    if data == "[DONE]":
                        return
                    yield TypeAdapter(response_type).validate_json(data, strict=True)
            for data in decoder.finish():
                if data != "[DONE]":
                    yield TypeAdapter(response_type).validate_json(data, strict=True)
        finally:
            await response.aclose()

    async def paginate(
        self,
        method: str,
        path: str,
        *,
        response_type: Any,
        items_field: str,
        token_field: str,
        token_param: str,
        page_size_param: str | None = None,
        max_pages: int | None = None,
        query: Mapping[str, Any] | None = None,
        body: Any = NOT_GIVEN,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
    ) -> AsyncIterator[Any]:
        del page_size_param
        params = dict(query or {})
        seen: set[str] = set()
        if params.get(token_param):
            seen.add(str(params[token_param]))
        count = 0
        if max_pages is not None and max_pages < 0:
            raise ValueError("max_pages must be nonnegative")
        while max_pages is None or count < max_pages:
            page = await self.request(
                method,
                path,
                response_type=response_type,
                query=params,
                body=body,
                headers=headers,
                idempotent=idempotent,
                idempotency_key=idempotency_key,
            )
            for item in _field(page, items_field) or []:
                yield item
            count += 1
            token = _field(page, token_field)
            if not token:
                return
            if not isinstance(token, str) or token in seen:
                raise ValueError("pagination returned a repeated or invalid cursor")
            seen.add(token)
            params[token_param] = token
