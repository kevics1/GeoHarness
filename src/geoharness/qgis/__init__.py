"""QGIS runs in its own installation's interpreter, never in the agent process."""

from .runner import execute_qgis

__all__ = ["execute_qgis"]
