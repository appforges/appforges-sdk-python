"""Authentication providers never log credentials."""

from __future__ import annotations

from typing import Protocol


class AuthProvider(Protocol):
    def apply(self, headers: dict[str, str]) -> None: ...


class AsyncAuthProvider(Protocol):
    async def apply(self, headers: dict[str, str]) -> None: ...


class BearerAuth:
    def __init__(self, token: str) -> None:
        self._token = token

    def apply(self, headers: dict[str, str]) -> None:
        headers["authorization"] = f"Bearer {self._token}"


class APIKeyAuth:
    def __init__(self, api_key: str, header: str = "x-api-key") -> None:
        self._api_key = api_key
        self._header = header

    def apply(self, headers: dict[str, str]) -> None:
        headers[self._header] = self._api_key
