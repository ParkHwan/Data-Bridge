"""Explicit failures raised by the embedding-provenance store contract."""


class EmbeddingProfileMismatchError(RuntimeError):
    """The requested vector operation is incompatible with stored provenance."""


class GenerationTargetError(RuntimeError):
    """The requested generation is absent or in the wrong initial state."""


class GenerationConcurrencyError(RuntimeError):
    """A generation changed after an operation's initial state check."""


class GenerationValidationError(RuntimeError):
    """A generation failed a structural or search-quality validation gate."""


class ValidationQueryConfigurationError(RuntimeError):
    """The validation query file is invalid or does not cover the target."""
