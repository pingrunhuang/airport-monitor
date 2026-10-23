"""Takeoff and landing monitor for Shenzhen Bao'an (ZGSZ).

The poll loop lives in ``python -m airport_monitor``. Detection is split
across ``ingest``, ``state_machine``, and ``alert``.
"""

__version__ = "0.1.0"
