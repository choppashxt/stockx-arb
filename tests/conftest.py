"""Shared test setup: make the project root importable no matter where
pytest is invoked from, and force UTF-8-safe console output on Windows."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
