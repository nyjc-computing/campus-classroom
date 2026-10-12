"""campus.apps.classroom.routes

API routes for Campus Classroom.

All data storage is handled by Campus API via campus-python client.
campus-classroom does not have its own database.
"""

from . import assignments, classroom_auth, classroom_sync, iframe, submissions

__all__ = ["assignments", "classroom_auth", "classroom_sync", "iframe",
           "submissions"]
