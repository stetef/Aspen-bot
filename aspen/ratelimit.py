"""Per-user rate limiting and in-flight concurrency helpers."""

import time
from typing import Optional

from . import config, state


def _check_rate_limit(uid: str) -> Optional[str]:
    """Return an error message if the user is rate-limited, else None."""
    now = time.time()
    with state._rate_lock:
        ts_list = state._rate_data[uid]
        ts_list[:] = [t for t in ts_list if now - t < config.RATE_LIMIT_WINDOW]
        if state._user_active[uid]:
            return "I'm still working on your previous request. I'll post results here when it's done."
        if len(ts_list) >= config.RATE_LIMIT_REQUESTS:
            mins = config.RATE_LIMIT_WINDOW // 60
            return (
                f"You've sent {config.RATE_LIMIT_REQUESTS} requests in the last {mins} minutes. "
                "Please wait before asking again."
            )
        ts_list.append(now)
        state._user_active[uid] = True
    return None


def _release_user(uid: str) -> None:
    with state._rate_lock:
        state._user_active[uid] = False
