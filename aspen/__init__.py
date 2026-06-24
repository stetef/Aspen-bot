"""
Aspen — HPC Slack Agent.

A backend-pluggable Slack agent: shared logic (config, tools, sessions, Slack
front-end) with interchangeable agent backends behind one async session interface.
``ASPEN_BACKEND`` selects the backend (``messages`` default, ``sdk`` later).
"""
