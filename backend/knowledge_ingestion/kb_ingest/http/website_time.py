"""Project explicitly owned website fields after the endpoint has finished its work."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from ..storage.file_time import time_order, utc_text
from ..storage.time_source import source_instant


def public_utc(value: object) -> str:
    """Split exact decimal seconds before formatting the calendar or fractional part."""
    whole, fraction = divmod(time_order(source_instant(value)), 1)
    date = utc_text(whole).partition(".")[0]
    digits = []
    while fraction:
        digit, fraction = divmod(10 * fraction, 1)
        digits.append(str(digit))
    return date + "." + "".join(digits).ljust(6, "0") + "Z"


type Endpoint = Callable[..., Any]
type Register = Callable[..., Callable[[Endpoint], Endpoint]]


@dataclass(frozen=True, slots=True)
class TimeFields:
    schema: dict[str, Any]
    project: Callable[[Any], Any]
    threaded: bool = False

    async def _project_async(self, value: Any) -> Any:
        if isinstance(value, Response):
            return value
        return await run_in_threadpool(self.project, value) if self.threaded else self.project(value)

    def documentation(self, status: int, *, description: str | None = None) -> dict[str, Any]:
        response: dict[str, Any] = {"content": {"application/json": {"schema": self.schema}}}
        if description is not None:
            response["description"] = description
        return {"responses": {str(status): response}}

    def route(self, method: Register, path: str, *, status_code: int | None = None) -> Callable[[Endpoint], Endpoint]:
        """Keep the registered signature and execution mode of each FastAPI handler."""

        def register(endpoint: Endpoint) -> Endpoint:
            if iscoroutinefunction(endpoint):

                @wraps(endpoint)
                async def asynchronous(*args: Any, **kwargs: Any) -> Any:
                    return await self._project_async(await endpoint(*args, **kwargs))

                handler: Endpoint = asynchronous
            else:

                @wraps(endpoint)
                def synchronous(*args: Any, **kwargs: Any) -> Any:
                    return self.project(endpoint(*args, **kwargs))

                handler = synchronous
            method(
                path, response_model=None, status_code=status_code, openapi_extra=self.documentation(status_code or 200)
            )(handler)
            return endpoint

        return register


def object_fields(**fields: TimeFields) -> TimeFields:
    def project(row: dict[str, Any]) -> dict[str, Any]:
        result = row
        for key, field in fields.items():
            if key in row and (value := field.project(row[key])) is not row[key]:
                if result is row:
                    result = row.copy()
                result[key] = value
        return result

    return TimeFields({"type": "object", "properties": {k: f.schema for k, f in fields.items()}}, project)


def sequence(item: TimeFields) -> TimeFields:
    def project(rows: list[Any]) -> list[Any]:
        result = rows
        for index, row in enumerate(rows):
            if (value := item.project(row)) is not row:
                if result is rows:
                    result = rows.copy()
                result[index] = value
        return result

    return TimeFields({"type": "array", "items": item.schema}, project)


def mapping(item: TimeFields) -> TimeFields:
    return TimeFields(
        {"type": "object", "additionalProperties": item.schema},
        lambda rows: {key: item.project(row) for key, row in rows.items()},
    )


def nullable(item: TimeFields) -> TimeFields:
    return TimeFields(
        {"anyOf": [item.schema, {"type": "null"}]}, lambda value: None if value is None else item.project(value)
    )


instant = TimeFields(
    {
        "type": "string",
        "format": "date-time",
        "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}([0-9]*[1-9])?Z$",
    },
    public_utc,
)
