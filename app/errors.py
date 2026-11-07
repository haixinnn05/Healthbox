from __future__ import annotations

from .reasons import message


class WorkflowError(Exception):
    """A rejected request. status: 409 invalid transition, 422 validation, 503 device unavailable."""

    def __init__(self, status: int, code: str, msg: str | None = None, **extra):
        super().__init__(msg or message(code))
        self.status = status
        self.code = code
        self.msg = msg or message(code)
        self.extra = extra
