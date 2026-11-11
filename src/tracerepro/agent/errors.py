"""Why an LLM call produced no completion. A model's own failure (a refusal, output cut off)
is not here: it is a `Completion` whose `model_failed` is true, and counts as an attempt."""


class LLMError(RuntimeError):
    """Base class: no completion was produced."""


class TransientError(LLMError):
    """A provider failure worth retrying (network, overload, timeout). Raised by providers."""


class InfraFailure(LLMError):
    """The call could not be made or its reply cannot be trusted. Never a model failure."""


class UsageLimitReached(LLMError):
    """The Claude plan's usage limit was reached. Stop now and resume after it resets."""


class PacingLimitReached(LLMError):
    """Our own usage guard refused the call (calls per hour or per day)."""

    def __init__(self, message: str, retry_at: float) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class TokenCapReached(LLMError):
    """The run's token cap is used up: the pipeline stops and submits its best patch."""
