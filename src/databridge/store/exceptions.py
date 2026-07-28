"""Explicit failures raised by the embedding-provenance store contract."""


class EmbeddingProfileMismatchError(RuntimeError):
    """The requested vector operation is incompatible with stored provenance."""
