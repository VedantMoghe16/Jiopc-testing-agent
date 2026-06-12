"""JioPC automated testing agent.

Validates a freshly patched JioPC OS image: web apps (Part A), native apps
(Part B), desktop/start-menu presence (Part C). Results are written to a
structured JSONL log; a separate model-agnostic LLM layer analyses the log
post-run.
"""

__version__ = "1.0.0"
