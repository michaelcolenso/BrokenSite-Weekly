"""
Retry utilities with exponential backoff for BrokenSite-Weekly.
"""

import time
import random
import functools
from typing import Callable, TypeVar, Any, Tuple, Type
import logging

from .config import RetryConfig

T = TypeVar("T")


def calculate_delay(
    attempt: int,
    config: RetryConfig,
) -> float:
    """
    Calculate delay for a given attempt number using exponential backoff.

    delay = min(base * (exponential_base ^ attempt), max_delay)
    Optional jitter adds randomness to prevent thundering herd.
    """
    delay = config.base_delay_seconds * (config.exponential_base ** attempt)
    delay = min(delay, config.max_delay_seconds)

    if config.jitter:
        # Add up to 25% jitter
        delay = delay * (0.75 + random.random() * 0.5)

    return delay


def retry_with_backoff(
    func: Callable[..., T],
    config: RetryConfig,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    logger: logging.Logger = None,
    operation_name: str = None,
) -> T:
    """
    Execute a function with retries and exponential backoff.

    Args:
        func: Zero-argument callable to execute
        config: Retry configuration
        exceptions: Tuple of exception types to catch and retry
        logger: Optional logger for retry messages
        operation_name: Human-readable name for logging

    Returns:
        The return value of func()

    Raises:
        The last exception if all retries are exhausted
    """
    op_name = operation_name or func.__name__
    last_exception = None

    for attempt in range(config.max_retries + 1):
        try:
            return func()
        except exceptions as e:
            last_exception = e

            if attempt < config.max_retries:
                delay = calculate_delay(attempt, config)
                if logger:
                    logger.warning(
                        f"{op_name} failed (attempt {attempt + 1}/{config.max_retries + 1}): "
                        f"{type(e).__name__}: {e}. Retrying in {delay:.1f}s"
                    )
                time.sleep(delay)
            else:
                if logger:
                    logger.error(
                        f"{op_name} failed after {config.max_retries + 1} attempts: "
                        f"{type(e).__name__}: {e}"
                    )

    raise last_exception


def retryable(
    config: RetryConfig = None,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    logger: logging.Logger = None,
):
    """
    Decorator to make a function retryable with exponential backoff.

    Usage:
        @retryable(config=retry_config, exceptions=(ConnectionError,))
        def fetch_data():
            ...
    """
    if config is None:
        config = RetryConfig()

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            return retry_with_backoff(
                func=lambda: func(*args, **kwargs),
                config=config,
                exceptions=exceptions,
                logger=logger,
                operation_name=func.__name__,
            )
        return wrapper
    return decorator


class RetryBudget:
    """
    Track retry budget across multiple operations.
    Useful for limiting total retries in a batch operation.
    """

    def __init__(self, max_total_retries: int = 50):
        self.max_total_retries = max_total_retries
        self.retries_used = 0

    def can_retry(self) -> bool:
        return self.retries_used < self.max_total_retries

    def use_retry(self):
        self.retries_used += 1

    @property
    def remaining(self) -> int:
        return max(0, self.max_total_retries - self.retries_used)
