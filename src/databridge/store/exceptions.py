"""Explicit failures raised by the embedding-provenance store contract."""


class EmbeddingProfileMismatchError(RuntimeError):
    """The requested vector operation is incompatible with stored provenance."""


class GenerationTargetError(RuntimeError):
    """The requested generation is absent or in the wrong initial state."""


class GenerationTargetNotFoundError(GenerationTargetError):
    """The requested generation does not exist in the selected space."""


class GenerationTargetStateMismatchError(GenerationTargetError):
    """The requested generation exists but is not in the required state."""


class GenerationConcurrencyError(RuntimeError):
    """A generation changed after an operation's initial state check."""


class GenerationValidationError(RuntimeError):
    """A generation failed a structural or search-quality validation gate."""


class StructuralGenerationValidationError(GenerationValidationError):
    """A generation failed structural validation."""


class SearchGenerationValidationError(GenerationValidationError):
    """A generation failed validation-query search checks."""


class ValidationQueryConfigurationError(RuntimeError):
    """The validation query file is invalid or does not cover the target."""


class ValidationQueryShaMismatchError(ValidationQueryConfigurationError):
    """The pinned validation query digest is invalid or does not match."""


class ActivationIntegrityError(GenerationTargetError):
    """The active generation lacks required activation evidence."""


class LegacyCleanupWindowClosedError(GenerationTargetError):
    """The active generation changed after its validation receipt was written."""
