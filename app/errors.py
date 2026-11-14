"""Shared application exceptions.

Exceptions carry no sensitive data (plaintext passwords, tokens, query results); messages are user-facing or logged.
"""


class GatewayError(Exception):
    """Base class for gateway business errors."""

    def __str__(self) -> str:
        from app.i18n import translate_error

        return translate_error(super().__str__())


class ConfigError(GatewayError):
    """Missing or invalid environment configuration."""


class DatabaseUnavailable(GatewayError):
    """Management database unavailable."""


class CryptoError(GatewayError):
    """Encryption/decryption or key format error."""


class ValidationError(GatewayError):
    """Form or service parameter validation failed."""


class NotFound(GatewayError):
    """Target record not found."""


class DataSourceConnectionError(GatewayError):
    """Business database connection test failed."""


class AuditUnavailableError(GatewayError):
    """Audit write or update failed; callers must block execution."""


class AdminUnauthorized(GatewayError):
    """Admin not signed in, session expired, or not authorized."""


class CSRFError(GatewayError):
    """CSRF validation failed."""
