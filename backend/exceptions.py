"""
Typed provider errors for candle and calendar data fetching.
Each error carries an http_code so routers can forward the right HTTP status.
"""


class ProviderError(Exception):
    """Base class for all data-provider errors."""
    def __init__(self, message: str, code: int = 502):
        super().__init__(message)
        self.http_code = code


class AuthError(ProviderError):
    """Raised when the API key is missing, invalid, or expired."""
    def __init__(self, provider: str):
        super().__init__(
            f"{provider}: Invalid or expired API key — "
            "check FX_API_KEY / CALENDAR_API_KEY in your .env file.",
            code=401,
        )


class RateLimitError(ProviderError):
    """Raised when the provider returns HTTP 429."""
    def __init__(self, provider: str):
        super().__init__(
            f"{provider}: API rate limit exceeded — "
            "wait before retrying or upgrade your plan.",
            code=429,
        )


class ProviderUnavailableError(ProviderError):
    """Raised when the provider is unreachable or returns an unexpected error."""
    def __init__(self, provider: str, detail: str = ""):
        msg = f"{provider}: Provider unavailable"
        if detail:
            msg += f" — {detail}"
        super().__init__(msg, code=502)


class EmptyResponseError(ProviderError):
    """Raised when the provider returns success but with no usable data."""
    def __init__(self, provider: str):
        super().__init__(
            f"{provider}: Provider returned no data for the requested range. "
            "Try a different timeframe or check your subscription tier.",
            code=502,
        )
