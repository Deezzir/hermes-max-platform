"""Hermes directory-plugin entry point for MAX."""

from .src.adapter import register

__all__ = ["register"]
