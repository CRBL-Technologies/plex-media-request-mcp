"""Errors shared by the media companion package.

The companion deliberately keeps configuration and transport failures typed.  In
particular, callers can turn a :class:`ConfigurationError` into a safe startup
diagnostic without ever interpolating a secret value into the diagnostic.
"""

from __future__ import annotations


class MediaCompanionError(Exception):
    """Base class for errors raised by companion-owned code."""


class ConfigurationError(MediaCompanionError, ValueError):
    """The process configuration is absent or invalid."""


class MissingConfigurationError(ConfigurationError):
    """A required configuration field was not supplied."""


class InvalidSecretReferenceError(ConfigurationError):
    """A secret was supplied in an unsupported or unsafe form.

    A secret reference error contains a field name only.  The value that failed
    validation is intentionally not retained in the exception.
    """


class InvalidURLConfigurationError(ConfigurationError):
    """An endpoint URL is not a permitted canonical HTTP(S) origin."""


class InvalidTimeoutConfigurationError(ConfigurationError):
    """A timeout is non-positive, inconsistent, or above its hard ceiling."""


class ModelValidationError(MediaCompanionError, ValueError):
    """A normalized model could not be constructed from trusted data."""


class AuthorizationError(MediaCompanionError):
    """An actor or private operation did not satisfy authorization policy."""


class ConfirmationRequiredError(AuthorizationError):
    """An authorized mutation needs the companion confirmation flow."""


class ConflictError(MediaCompanionError):
    """A compare-and-swap or idempotency operation observed conflicting state."""


class NotFoundError(MediaCompanionError):
    """A requested normalized record does not exist."""


class DependencyError(MediaCompanionError):
    """A configured external dependency failed or returned unusable data."""


# Short aliases make the small package pleasant to consume while preserving the
# explicit names used by startup callers.
ConfigError = ConfigurationError
ValidationError = ModelValidationError
InvalidConfigError = ConfigurationError


__all__ = [
    "AuthorizationError",
    "ConfigError",
    "ConfigurationError",
    "ConflictError",
    "ConfirmationRequiredError",
    "DependencyError",
    "InvalidConfigError",
    "InvalidSecretReferenceError",
    "InvalidTimeoutConfigurationError",
    "InvalidURLConfigurationError",
    "MediaCompanionError",
    "MissingConfigurationError",
    "ModelValidationError",
    "NotFoundError",
    "ValidationError",
]
