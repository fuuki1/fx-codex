"""Operational tools (monitoring, collectors, scorecards, launchd helpers).

This ``__init__`` exists so ``tools.X`` imports resolve to one canonical
module name under mypy (otherwise files are seen twice: ``X`` and
``tools.X``). Tools remain individually executable as scripts.
"""
