"""The LiveKit agent worker package.

The backend package lives under ``backend/``, so it has to be on the
import path before anything here can import it. The editable install adds
the project root but not ``backend/``, and relying on the install alone
makes the worker fail with a bare ``ModuleNotFoundError`` that says
nothing about the cause. Putting it here makes it work every time.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _PROJECT_ROOT / "backend"

if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
