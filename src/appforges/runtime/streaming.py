"""Incremental SSE framing independent of HTTP chunk and UTF-8 boundaries."""

from __future__ import annotations

from collections.abc import Iterator


class SSEDecoder:
    def __init__(self) -> None:
        self._data: list[str] = []

    def line(self, line: str) -> Iterator[str]:
        if line == "":
            if self._data:
                yield "\n".join(self._data)
                self._data.clear()
        elif line.startswith("data:"):
            value = line[5:]
            self._data.append(value.removeprefix(" "))

    def finish(self) -> Iterator[str]:
        if self._data:
            yield "\n".join(self._data)
            self._data.clear()
