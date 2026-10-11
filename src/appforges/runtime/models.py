"""Strict wire models; omitted values differ from explicit JSON null."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class SDKModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", populate_by_name=True)


class NotGiven:
    """A method argument that was omitted by the caller."""

    def __repr__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN = NotGiven()


def non_null(value: Any) -> Any:
    """Reject explicit null for optional non-nullable wire fields.

    An absent field uses its unvalidated None default and is excluded on the wire.
    """
    if value is None:
        raise ValueError("field does not accept explicit null")
    return value


def wire_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_unset=True, by_alias=True)
    if isinstance(value, dict):
        return {k: wire_value(v) for k, v in value.items() if not isinstance(v, NotGiven)}
    if isinstance(value, (list, tuple)):
        return [wire_value(v) for v in value]
    return value
