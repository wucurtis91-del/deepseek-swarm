"""Shared runtime for deepseek-swarm.

`swarm.py` (the original one-shot runner) and `swarm_loop.py` (the persistent
decision loop) both use this package for process control, redaction, state and
model backends. Standard library only.
"""

__version__ = "2.0.0"
SCHEMA_VERSION = 1
