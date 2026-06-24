"""Process-wide in-memory state for rate limiting and global concurrency.

Conversation history lives in ``sessions.py``; this module holds only the
rate-limit bookkeeping and the global execution semaphore.
"""

from collections import defaultdict
from threading import Lock, Semaphore

from . import config

_rate_lock = Lock()
_rate_data: dict[str, list[float]] = defaultdict(list)   # uid → [timestamps]
_user_active: dict[str, bool] = defaultdict(bool)        # uid → in-flight?

_global_sem = Semaphore(config.MAX_CONCURRENT)
