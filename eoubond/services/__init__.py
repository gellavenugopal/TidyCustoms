"""Business logic. Every service works inside the caller's session/transaction and
never commits; the caller commits on success and rolls back on any exception,
so a FIFO allocation and its bond ledger entries are always atomic."""
from .. import audit  # noqa: F401  (make sure the audit listener is registered)
