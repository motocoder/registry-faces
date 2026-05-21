"""Provider interface.

Each provider knows how to run an agentic loop against one LLM API. The same
tool functions (plain Python) are passed to every provider; each provider
converts them to its native format and dispatches calls.

Providers MUST be self-contained: the agent layer should not need to know
which one is running.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable


class Provider(ABC):
    """A configured LLM that can run a tool-using agent loop."""

    name: str = ""
    model: str = ""

    @abstractmethod
    def run_agent(
        self,
        *,
        system: str,
        user_prompt: str,
        tools: list[Callable],
        max_iterations: int = 15,
    ) -> str:
        """Run the agent loop. Returns the final assistant text response."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r})"
