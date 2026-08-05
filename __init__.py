"""Hermes directory-plugin entry point for MAX."""

from .src.max_hermes_plugin.adapter import register

__all__ = ["register"]
