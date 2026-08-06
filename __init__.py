"""Hermes directory-plugin entry point for MAX."""

if __package__:
    from .src.adapter import register
else:
    from src.adapter import register

__all__ = ["register"]
