"""AWS CloudFront invalidation provider.

Uses boto3, which is synchronous; calls are dispatched via
:func:`asyncio.to_thread`. See ``docs/cdn-providers.md`` for API details
and error classification.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    PartialCredentialsError,
)

from mirror_mirage.providers.base import PurgeResult

_RETRYABLE_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "TooManyInvalidationsInProgress",
        "InternalFailure",
        "InternalServerError",
    }
)
# CloudFront caps the paths being invalidated at once per distribution
# (3,000 file paths). Hitting the cap means "wait", not "failed": the
# provider holds the batch and resubmits until there is room.
_IN_PROGRESS_LIMIT_CODE = "TooManyInvalidationsInProgress"
CLOUDFRONT_IN_PROGRESS_POLL_SECONDS = 30.0
CLOUDFRONT_IN_PROGRESS_MAX_WAIT_SECONDS = 1800.0
_log = logging.getLogger("mirror_mirage.cloudfront")


class CloudFrontProvider:
    """Purges paths via the CloudFront ``create_invalidation`` API."""

    def __init__(
        self,
        *,
        distribution_id: str,
        region: str = "us-east-1",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        in_progress_poll_seconds: float = CLOUDFRONT_IN_PROGRESS_POLL_SECONDS,
        in_progress_max_wait_seconds: float = CLOUDFRONT_IN_PROGRESS_MAX_WAIT_SECONDS,
    ) -> None:
        """Create a provider for a single CloudFront distribution.

        ``access_key_id`` and ``secret_access_key`` may be ``None`` — in
        which case boto3 will fall back to its default credential chain
        (instance role, env vars, etc.).

        When CloudFront rejects a batch with ``TooManyInvalidationsInProgress``,
        :meth:`purge` resubmits it every ``in_progress_poll_seconds`` for up
        to ``in_progress_max_wait_seconds`` before returning a retryable
        failure.
        """
        self._distribution_id = distribution_id
        self._region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._in_progress_poll_seconds = in_progress_poll_seconds
        self._in_progress_max_wait_seconds = in_progress_max_wait_seconds
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            kwargs: dict[str, Any] = {"region_name": self._region}
            if self._access_key_id is not None:
                kwargs["aws_access_key_id"] = self._access_key_id
                kwargs["aws_secret_access_key"] = self._secret_access_key
            self._client = boto3.client("cloudfront", **kwargs)
        return self._client

    async def purge(self, urls: list[str]) -> PurgeResult:
        """Submit one invalidation containing every URL's path.

        URLs are converted to paths (the scheme/host are stripped) before
        being sent to CloudFront. The boto3 call runs inside
        :func:`asyncio.to_thread` so the event loop is not blocked.

        If the distribution is at CloudFront's in-progress invalidation
        limit, the call waits and resubmits instead of failing, which
        pauses this CDN's queue until CloudFront has room. Each CDN has
        one queue worker, so no other job uses up retry attempts while
        the limit is hit.
        """
        if not urls:
            return PurgeResult(ok=True, retryable=False, message="empty")

        # Deduplicate paths; CloudFront bills per path so dedup matters.
        paths = sorted({urlparse(u).path or "/" for u in urls})
        caller_ref = f"mirror-mirage-{time.time_ns()}"

        def _call() -> Any:
            client = self._get_client()
            return client.create_invalidation(
                DistributionId=self._distribution_id,
                InvalidationBatch={
                    "Paths": {"Quantity": len(paths), "Items": paths},
                    "CallerReference": caller_ref,
                },
            )

        loop = asyncio.get_running_loop()
        started = loop.time()
        rejections = 0
        while True:
            try:
                await asyncio.to_thread(_call)
                break
            except (NoCredentialsError, PartialCredentialsError) as e:
                # Missing or half-set AWS credentials won't fix themselves —
                # operator must update secrets.yaml (or the instance profile
                # / ~/.aws/credentials, depending on how creds are sourced).
                return PurgeResult(ok=False, retryable=False, message=f"auth: {e}")
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                waited = loop.time() - started
                if code != _IN_PROGRESS_LIMIT_CODE:
                    return PurgeResult(
                        ok=False,
                        retryable=code in _RETRYABLE_CODES,
                        message=f"{code}: {e}",
                    )
                if waited >= self._in_progress_max_wait_seconds:
                    return PurgeResult(
                        ok=False,
                        retryable=True,
                        message=f"{code}: still at the limit after waiting {waited:.0f}s: {e}",
                    )
            except BotoCoreError as e:
                return PurgeResult(ok=False, retryable=True, message=f"botocore: {e}")
            except Exception as e:
                return PurgeResult(ok=False, retryable=True, message=f"transport: {e}")

            # The distribution is at CloudFront's cap on paths being
            # invalidated at once. Hold this batch (and with it this CDN's
            # queue) and resubmit once earlier invalidations have had time
            # to finish, rather than failing the attempt. The resubmit
            # reuses ``caller_ref``, so it can never create a duplicate.
            rejections += 1
            if rejections == 1:
                _log.warning(
                    "event=cloudfront_in_progress_limit distribution_id=%s paths=%d "
                    "poll_s=%.0f max_wait_s=%.0f",
                    self._distribution_id,
                    len(paths),
                    self._in_progress_poll_seconds,
                    self._in_progress_max_wait_seconds,
                )
            await asyncio.sleep(
                min(self._in_progress_poll_seconds, self._in_progress_max_wait_seconds - waited)
            )

        if rejections:
            _log.info(
                "event=cloudfront_in_progress_cleared distribution_id=%s paths=%d "
                "waited_s=%.0f resubmits=%d",
                self._distribution_id,
                len(paths),
                loop.time() - started,
                rejections,
            )
        return PurgeResult(ok=True, retryable=False, message="ok")
