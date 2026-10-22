"""Make the backend package importable from the repository root.

The backend lives under ``backend/`` so that its own directory holds only
application code and tests. The editable install puts the project root on
the import path, but not ``backend/``, so the tests would fail to import
``app`` from a plain ``pytest`` run at the root. Adding it here means
``pytest`` works from anywhere in the repository.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
_BACKEND = _PROJECT_ROOT / "backend"

if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
