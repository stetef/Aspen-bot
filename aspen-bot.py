#!/usr/bin/env python3
"""
Aspen — HPC Slack Agent (launcher).

The implementation now lives in the importable ``aspen/`` package; this file is a
thin launcher so existing tooling (start.sh, systemd) keeps working unchanged.
"""

from aspen.main import main

if __name__ == "__main__":
    main()
