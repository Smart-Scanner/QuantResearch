"""Pytest bootstrap.

Ensures the repository root is on ``sys.path`` so tests located under
``tests/`` can ``import db`` / ``from intelligence import ...`` / etc.
exactly as they did when they lived at the repo root. This is test
infrastructure only — it does not alter any application behaviour.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
