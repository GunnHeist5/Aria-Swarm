"""JustCall integration: API client, campaign sync, inbound webhook handling,
and the writeback outbox. Public surface per docs/architecture.md
(orchestrator/justcall/ section) — signatures there are binding."""

from .client import JustCallClient, JustCallError
from .sync import SyncStats, discover_company_field, extract_company_name, run_sync
from .webhook import handle_call_completed, verify_signature
from .writeback import enqueue_writeback, process_outbox_batch

__all__ = [
    "JustCallClient",
    "JustCallError",
    "SyncStats",
    "discover_company_field",
    "extract_company_name",
    "run_sync",
    "handle_call_completed",
    "verify_signature",
    "enqueue_writeback",
    "process_outbox_batch",
]
