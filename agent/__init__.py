"""StorySpark ADK agent package."""

__all__ = ["storyteller_agent"]


def __getattr__(name: str):
    if name == "storyteller_agent":
        from .storyteller_agent import storyteller_agent

        return storyteller_agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
