#!/usr/bin/env python3
"""Repository entrypoint; installed skills call the same CLI directly."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "skills" / "jira-achievements" / "scripts"))
from jira_achievements import main

if __name__ == "__main__":
    sys.exit(main())
