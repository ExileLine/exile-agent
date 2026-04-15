class AIRuntimeError(Exception):
    """Base AI runtime error."""


class AIDisabledError(AIRuntimeError):
    """Raised when AI features are disabled."""


class AIRuntimeNotReadyError(AIRuntimeError):
    """Raised when AI runtime resources are not available."""


class AgentNotFoundError(AIRuntimeError):
    """Raised when an agent id is not registered."""
