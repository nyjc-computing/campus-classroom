"""campus.apps.classroom.routes

API routes for Campus Classroom.

All data storage is handled by Campus API via campus-python client.
campus-classroom does not have its own database.
"""

from . import assignments
from . import submissions

__all__ = ["assignments", "submissions"]
