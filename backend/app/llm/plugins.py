"""The LiveKit LLM plugins, imported on the main thread.

A LiveKit plugin registers itself at import time, and the registration is
rejected from a worker thread. Importing the plugins here, at module load,
puts that on the main thread of whichever process imports this module --
the API or the agent worker -- and every later import is a cached lookup.

The split matters for startup time as well as for correctness. The
Google plugin costs a second or more to import and the agent worker
never uses it, so the two are kept apart:

* :mod:`app.llm.groq_plugin` -- the chat model, used by the voice tutor.
* :mod:`app.llm.google_plugin` -- the notes model, used by the pipeline.
"""

from __future__ import annotations

# Importing a plugin registers it. The first import has to be on the main
# thread, so both modules are imported here rather than inside a function.
from livekit.plugins import google as google_plugin  # noqa: F401
from livekit.plugins import groq as groq_plugin  # noqa: F401

__all__ = ["google_plugin", "groq_plugin"]
