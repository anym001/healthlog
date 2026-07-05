"""LLM narration of the current findings snapshot via a local Ollama instance.

Reads the ``findings`` table, renders it as a compact statistical context
(z-scores, slopes, ratios and coefficients — plus the measured value where a
finding carries one, e.g. an anomaly's reading), and calls Ollama's
``/api/chat`` endpoint to produce a weekly health report. The context is sent
only to the operator-configured endpoint (``narrate.ollama_url``) — run Ollama
on your own hardware to keep the data inside your network.

Usage::

    docker exec healthlog healthlog narrate
    docker exec healthlog healthlog narrate --note "Focus on the HRV/training link."
    docker exec healthlog healthlog narrate --lookback-days 14 --language en

The report is written to ``/config/narration/YYYY-MM-DD.md`` and printed to
stdout. Configure the Ollama endpoint and model under ``narrate:`` in
``config.yaml``.

The module is split by role:

- ``prompts`` — the per-language system prompts (code artefacts, not config)
- ``context`` — findings → plain-text context for the model (``scrub_details``
  is a deliberate pass-through today; the hook for a future privacy mode)
- ``loader`` — the findings SQL query
- ``client`` — the Ollama HTTP client
- ``cli`` — argument parsing + the ``narrate`` command entry point

The flat public API is re-exported here so ``from app.narrate import …`` and
``app.narrate.run`` keep working. ``report_priority`` (and its helpers
``_metric_domain``/``_pair_tier``) live in the analysis package next to the
metric taxonomy; they are re-exported here for the narration tests.
"""

from __future__ import annotations

from ..analysis import _metric_domain, _pair_tier, report_priority
from .cli import add_arguments, run, write_report
from .client import OllamaClient
from .context import build_context, scrub_details
from .loader import load_findings
from .prompts import _system_prompt

__all__ = [
    "OllamaClient",
    "_metric_domain",
    "_pair_tier",
    "_system_prompt",
    "add_arguments",
    "build_context",
    "load_findings",
    "report_priority",
    "run",
    "scrub_details",
    "write_report",
]
