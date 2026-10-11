"""OAuth PKCE, refresh rotation and RFC 8628 device grants."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from pydantic import Field

from .models import SDKModel


@dataclass(frozen=True)
class PKCE:
    verifier: str
    state: str
    challenge: str


def create_pkce() -> PKCE:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return PKCE(verifier, secrets.token_urlsafe(32), challenge)


def authorization_url(
    authorize_url: str, client_id: str, redirect_uri: str, pkce: PKCE, scope: str | None = None
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": pkce.state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        params["scope"] = scope
    return authorize_url + ("&" if "?" in authorize_url else "?") + urlencode(params)


class OAuthTokens(SDKModel):
    access_token: str = Field(repr=False)
    token_type: str = "Bearer"
    refresh_token: str | None = Field(default=None, repr=False)
    expires_at: float | None = None
    scope: str | None = None


class DeviceAuthorization(SDKModel):
    device_code: str = Field(repr=False)
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None
    interval: float = Field(default=5.0, gt=0)
    expires_at: float | None = None


DevicePollResult = OAuthTokens | Literal["pending", "slow_down", "expired"]


def _tokens(payload: Any) -> OAuthTokens:
    if not isinstance(payload, dict):
        raise TypeError("invalid OAuth token response")
    values = dict(payload)
    expires_in = values.pop("expires_in", None)
    if isinstance(expires_in, (int, float)):
        values["expires_at"] = time.time() + expires_in
    return OAuthTokens.model_validate(values)


def _device(payload: Any) -> DeviceAuthorization:
    if not isinstance(payload, dict):
        raise TypeError("invalid device authorization response")
    values = dict(payload)
    values.setdefault("verification_uri", values.get("verification_url"))
    expires_in = values.pop("expires_in", None)
    if isinstance(expires_in, (int, float)):
        values["expires_at"] = time.time() + expires_in
    return DeviceAuthorization.model_validate(values)


def _poll(response: httpx.Response) -> DevicePollResult:
    payload = response.json()
    if response.is_success:
        return _tokens(payload)
    code = payload.get("error") if isinstance(payload, dict) else None
    result: dict[str, Literal["pending", "slow_down", "expired"]] = {
        "authorization_pending": "pending",
        "slow_down": "slow_down",
        "expired_token": "expired",
    }
    if isinstance(code, str) and code in result:
        return result[code]
    raise ValueError(f"device authorization failed with HTTP {response.status_code}")


class OAuthClient:
    def __init__(
        self, token_url: str, client_id: str, http_client: httpx.Client | None = None
    ) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self._owns_client = http_client is None
        self._http = http_client if http_client is not None else httpx.Client(timeout=60)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def _grant(self, values: dict[str, str]) -> OAuthTokens:
        response = self._http.post(self.token_url, data={"client_id": self.client_id, **values})
        if not response.is_success:
            raise ValueError(f"OAuth grant failed with HTTP {response.status_code}")
        return _tokens(response.json())

    def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> OAuthTokens:
        return self._grant(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
        )

    def refresh(self, refresh_token: str) -> OAuthTokens:
        return self._grant({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def start_device(self, device_url: str, scope: str | None = None) -> DeviceAuthorization:
        data = {"client_id": self.client_id}
        if scope:
            data["scope"] = scope
        response = self._http.post(device_url, data=data)
        if not response.is_success:
            raise ValueError(f"device authorization failed with HTTP {response.status_code}")
        return _device(response.json())

    def poll_device(self, authorization: DeviceAuthorization) -> DevicePollResult:
        response = self._http.post(
            self.token_url,
            data={
                "client_id": self.client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": authorization.device_code,
            },
        )
        return _poll(response)

    def wait_device(
        self,
        authorization: DeviceAuthorization,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> OAuthTokens:
        interval = authorization.interval
        while True:
            if authorization.expires_at is not None and clock() >= authorization.expires_at:
                raise TimeoutError("device authorization expired")
            sleep(interval)
            if authorization.expires_at is not None and clock() >= authorization.expires_at:
                raise TimeoutError("device authorization expired")
            result = self.poll_device(authorization)
            if isinstance(result, OAuthTokens):
                return result
            if result == "expired":
                raise TimeoutError("device authorization expired")
            if result == "slow_down":
                interval += 5


class AsyncOAuthClient:
    def __init__(
        self, token_url: str, client_id: str, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self._owns_client = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient(timeout=60)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def _grant(self, values: dict[str, str]) -> OAuthTokens:
        response = await self._http.post(
            self.token_url, data={"client_id": self.client_id, **values}
        )
        if not response.is_success:
            raise ValueError(f"OAuth grant failed with HTTP {response.status_code}")
        return _tokens(response.json())

    async def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> OAuthTokens:
        return await self._grant(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        return await self._grant({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def start_device(self, device_url: str, scope: str | None = None) -> DeviceAuthorization:
        data = {"client_id": self.client_id}
        if scope:
            data["scope"] = scope
        response = await self._http.post(device_url, data=data)
        if not response.is_success:
            raise ValueError(f"device authorization failed with HTTP {response.status_code}")
        return _device(response.json())

    async def poll_device(self, authorization: DeviceAuthorization) -> DevicePollResult:
        response = await self._http.post(
            self.token_url,
            data={
                "client_id": self.client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": authorization.device_code,
            },
        )
        return _poll(response)

    async def wait_device(
        self,
        authorization: DeviceAuthorization,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> OAuthTokens:
        interval = authorization.interval
        while True:
            if authorization.expires_at is not None and clock() >= authorization.expires_at:
                raise TimeoutError("device authorization expired")
            await sleep(interval)
            if authorization.expires_at is not None and clock() >= authorization.expires_at:
                raise TimeoutError("device authorization expired")
            result = await self.poll_device(authorization)
            if isinstance(result, OAuthTokens):
                return result
            if result == "expired":
                raise TimeoutError("device authorization expired")
            if result == "slow_down":
                interval += 5


class RefreshableBearer:
    def __init__(self, tokens: OAuthTokens, oauth_client: OAuthClient) -> None:
        self.tokens = tokens
        self._oauth = oauth_client
        self._lock = threading.Lock()

    def apply(self, headers: dict[str, str]) -> None:
        with self._lock:
            if self.tokens.expires_at is not None and self.tokens.expires_at - time.time() < 30:
                refresh = self.tokens.refresh_token
                if refresh is None:
                    raise ValueError("token refresh requires a refresh token")
                new = self._oauth.refresh(refresh)
                if new.refresh_token is None:
                    new.refresh_token = refresh
                self.tokens = new
            headers["authorization"] = f"Bearer {self.tokens.access_token}"


class AsyncRefreshableBearer:
    def __init__(self, tokens: OAuthTokens, oauth_client: AsyncOAuthClient) -> None:
        self.tokens = tokens
        self._oauth = oauth_client
        self._lock = asyncio.Lock()

    async def apply(self, headers: dict[str, str]) -> None:
        async with self._lock:
            if self.tokens.expires_at is not None and self.tokens.expires_at - time.time() < 30:
                refresh = self.tokens.refresh_token
                if refresh is None:
                    raise ValueError("token refresh requires a refresh token")
                new = await self._oauth.refresh(refresh)
                if new.refresh_token is None:
                    new.refresh_token = refresh
                self.tokens = new
            headers["authorization"] = f"Bearer {self.tokens.access_token}"
