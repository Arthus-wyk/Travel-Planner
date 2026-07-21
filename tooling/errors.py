class ToolError(Exception):
    """Base class for errors safe to expose at the tool boundary."""


class ToolNotFoundError(ToolError):
    pass


class ToolValidationError(ToolError):
    pass


class ToolPolicyError(ToolError):
    pass
