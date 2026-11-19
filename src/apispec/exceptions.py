"""Exception classes."""


class APISpecError(Exception):
    """Base class for all apispec-related errors."""


class PluginMethodNotImplementedError(APISpecError, NotImplementedError):
    """Raised when calling an unimplemented helper method in a plugin"""


class DuplicateComponentNameError(APISpecError):
    """Raised when registering two components with the same name"""


class DuplicateParameterError(APISpecError):
    """Raised when registering a parameter already existing in a given scope"""


class InvalidParameterError(APISpecError):
    """Raised when parameter doesn't contains required keys"""


class OpenAPIError(APISpecError):
    """Raised when a OpenAPI spec validation fails."""


class ExampleValidationError(APISpecError):
    """Raised when example values do not match their schemas.

    :param issues: The collected :class:`apispec.examples.ExampleValidationIssue`
        objects.
    """

    def __init__(self, issues) -> None:
        self.issues = self.errors = list(issues)
        details = "\n".join(f"- {issue}" for issue in self.issues)
        super().__init__(f"Invalid example values:\n{details}")

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else self.__class__.__name__
