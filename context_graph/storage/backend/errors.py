"""Backend-specific exception types for the additive storage backend."""


class BackendConfigurationError(RuntimeError):
    """Raised when a backend cannot be configured with the provided settings."""


class UnsupportedCapabilityError(RuntimeError):
    """Raised when the selected backend lacks a requested capability."""


class BackendFeatureNotImplementedError(NotImplementedError):
    """Raised when a supported interface method is intentionally stubbed in V1."""
