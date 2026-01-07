"""
Retry utilities with exponential backoff for YouTube operations.

This module provides decorators and utilities for retrying operations
that may fail due to transient errors like rate limiting or network issues.
"""

import functools
import logging
import random
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def is_retryable_error(error_msg: str) -> bool:
    """
    Check if an error message indicates a retryable error.

    Args:
        error_msg: The error message to check

    Returns:
        True if the error is retryable, False otherwise
    """
    error_lower = error_msg.lower()
    retryable_keywords = [
        "429",
        "rate limit",
        "too many requests",
        "temporary",
        "timeout",
        "connection",
        "network",
        "server error",
        "502",
        "503",
        "504",
    ]
    return any(keyword in error_lower for keyword in retryable_keywords)


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 2.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable:
    """
    Decorator for retrying functions with exponential backoff.

    This decorator will catch exceptions from the decorated function and
    retry the function call with increasing delays between attempts.
    Only retries if the error message indicates a retryable error
    (rate limiting, timeouts, etc.).

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay between retries in seconds (default: 2.0)
        max_delay: Maximum delay between retries in seconds (default: 30.0)
        backoff_factor: Multiplier for delay after each retry (default: 2.0)
        jitter: Add random jitter to delay to avoid thundering herd (default: True)
        retryable_exceptions: Tuple of exception types that trigger retry

    Returns:
        Decorated function with retry logic

    Example:
        @retry_with_backoff(max_retries=3, initial_delay=2.0)
        def fetch_data():
            # This function will be retried up to 3 times
            # with delays of ~2s, ~4s, ~8s between attempts
            pass
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            delay = initial_delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e
                    error_msg = str(e)

                    # Check if this specific error is retryable
                    if not is_retryable_error(error_msg):
                        logger.debug(
                            f"Non-retryable error in {func.__name__}: {e}"
                        )
                        raise

                    # Don't retry if we've exhausted attempts
                    if attempt >= max_retries:
                        logger.warning(
                            f"Max retries ({max_retries}) exceeded for {func.__name__}: {e}"
                        )
                        raise

                    # Calculate delay with optional jitter
                    current_delay = min(delay, max_delay)
                    if jitter:
                        # Add +/- 50% jitter
                        current_delay *= 0.5 + random.random()

                    logger.warning(
                        f"Attempt {attempt + 1}/{max_retries + 1} failed for {func.__name__}: {e}. "
                        f"Retrying in {current_delay:.1f}s..."
                    )

                    time.sleep(current_delay)
                    delay *= backoff_factor

            # Should never reach here, but just in case
            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected error in retry logic")

        return wrapper

    return decorator


class RetryContext:
    """
    Context manager for manual retry logic with exponential backoff.

    Useful when you need more control over the retry process than
    the decorator provides.

    Example:
        with RetryContext(max_retries=3) as ctx:
            while ctx.should_retry():
                try:
                    result = do_something()
                    break
                except SomeError as e:
                    ctx.record_failure(e)
    """

    def __init__(
        self,
        max_retries: int = 3,
        initial_delay: float = 2.0,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
        jitter: bool = True,
    ):
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.jitter = jitter
        self.attempt = 0
        self.delay = initial_delay
        self.last_error: Exception | None = None

    def __enter__(self) -> "RetryContext":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def should_retry(self) -> bool:
        """Check if we should attempt another retry."""
        return self.attempt <= self.max_retries

    def record_failure(self, error: Exception) -> None:
        """
        Record a failure and wait before next retry.

        Args:
            error: The exception that was raised
        """
        self.last_error = error
        self.attempt += 1

        if self.attempt <= self.max_retries:
            current_delay = min(self.delay, self.max_delay)
            if self.jitter:
                current_delay *= 0.5 + random.random()

            logger.warning(
                f"Attempt {self.attempt}/{self.max_retries + 1} failed: {error}. "
                f"Retrying in {current_delay:.1f}s..."
            )

            time.sleep(current_delay)
            self.delay *= self.backoff_factor

    def get_last_error(self) -> Exception | None:
        """Get the last recorded error."""
        return self.last_error
