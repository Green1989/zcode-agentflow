"""ZCode-AgentFlow package.

User-level plugin: ZCode (GLM-5.2) supervisor/executor, Codex (GPT) planner,
Grok (Grok-4.5) reviewer, coordinated via versioned files + JSON Schema.

This module exposes only the public surface the package contract requires at
Phase 1 (the package version). Broader re-exports land with the modules that
introduce them in later phases.
"""

__version__ = "0.1.0"
