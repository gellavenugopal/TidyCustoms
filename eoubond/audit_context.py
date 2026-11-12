"""Who is acting right now (set by the web layer, CLI or tests)."""
from contextlib import contextmanager
from contextvars import ContextVar

_current_user: ContextVar[str | None] = ContextVar("eoubond_current_user", default=None)


def current_username() -> str:
    return _current_user.get() or "system"


def set_current_user(username: str | None):
    return _current_user.set(username)


@contextmanager
def acting_as(username: str):
    token = _current_user.set(username)
    try:
        yield
    finally:
        _current_user.reset(token)
