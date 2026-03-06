"""Compatibility entrypoint for deployment tooling.

Some deploy scripts import `backend.interactive_storyteller_app:app`.
This module re-exports the FastAPI app defined in `backend.main`.
"""

from .main import app

__all__ = ["app"]
