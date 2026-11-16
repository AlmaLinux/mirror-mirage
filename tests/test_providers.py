"""Tests for :mod:`mirror_mirage.providers`.

Verifies request shape, batching, and error classification for each
provider against the rules in ``docs/cdn-providers.md``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from mirror_mirage.providers.base import PurgeResult
from mirror_mirage.providers.cloudfront import CloudFrontProvider
from mirror_mirage.providers.dry_run import DryRunProvider
from mirror_mirage.providers.fastly import FastlyProvider
from mirror_mirage.providers.gcp import GcpCdnProvider

# ---------------------------------------------------------------------------
# Fastly
# ---------------------------------------------------------------------------


class TestFastlyProvider:
    """Per-URL purge: POST https://api.fastly.com/purge/{host}{path}."""

    @respx.mock
    async def test_calls_per_url_purge_endpoint(self) -> None:
        # One route per URL.
        r_a = respx.post("https://api.fastly.com/purge/repo.example.com/a").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        r_b = respx.post("https://api.fastly.com/purge/repo.example.com/b").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a", "https://repo.example.com/b"])
        assert result.ok is True
        assert r_a.called and r_b.called

    @respx.mock
    async def test_one_call_per_url(self) -> None:
        route = respx.post(
            url__regex=r"https://api\.fastly\.com/purge/repo\.example\.com/\d+"
        ).mock(return_value=httpx.Response(200, json={"status": "ok"}))
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            urls = [f"https://repo.example.com/{i}" for i in range(20)]
            result = await p.purge(urls)
        assert result.ok is True
        assert route.call_count == 20

    @respx.mock
    async def test_sends_token_in_header(self) -> None:
        route = respx.post("https://api.fastly.com/purge/repo.example.com/a").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            await p.purge(["https://repo.example.com/a"])
        assert route.calls[0].request.headers.get("Fastly-Key") == "fxxx-test"

    @respx.mock
    async def test_query_string_preserved(self) -> None:
        route = respx.post("https://api.fastly.com/purge/repo.example.com/x?v=1").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            await p.purge(["https://repo.example.com/x?v=1"])
        assert route.called

    @respx.mock
    async def test_429_is_retryable(self) -> None:
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(429, json={"msg": "rate limited"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is False
        assert result.retryable is True

    @respx.mock
    async def test_500_is_retryable(self) -> None:
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(503)
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a"])
        assert result.retryable is True

    @respx.mock
    async def test_401_is_terminal(self) -> None:
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(401)
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is False
        assert result.retryable is False

    @respx.mock
    async def test_transport_error_is_retryable(self) -> None:
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            side_effect=httpx.ConnectError("boom")
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a"])
        assert result.retryable is True

    @respx.mock
    async def test_mixed_results_reported_per_url(self) -> None:
        # Each URL's outcome is kept so the worker can retry just the
        # retryable ones; the batch is retryable if any URL is.
        respx.post("https://api.fastly.com/purge/repo.example.com/a").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        respx.post("https://api.fastly.com/purge/repo.example.com/b").mock(
            return_value=httpx.Response(401)
        )
        respx.post("https://api.fastly.com/purge/repo.example.com/c").mock(
            return_value=httpx.Response(503)
        )
        urls = [
            "https://repo.example.com/a",
            "https://repo.example.com/b",
            "https://repo.example.com/c",
        ]
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(urls)
        assert result.ok is False
        assert result.retryable is True
        assert result.per_url is not None
        assert list(result.per_url) == urls
        a, b, c = (result.per_url[u] for u in urls)
        assert a.ok is True
        assert (b.ok, b.retryable, b.message) == (False, False, "HTTP 401")
        assert (c.ok, c.retryable, c.message) == (False, True, "HTTP 503")

    @respx.mock
    async def test_wildcard_skipped_link_url_purged(self, caplog) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="mirror_mirage.fastly")
        # Only the link's own URL has a route; a call for the wildcard
        # would hit respx's "not mocked" error.
        route = respx.post("https://api.fastly.com/purge/repo.example.com/almalinux/10").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        urls = ["https://repo.example.com/almalinux/10", "https://repo.example.com/almalinux/10/*"]
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(urls)
        assert result.ok is True
        assert route.call_count == 1
        assert result.per_url is not None
        assert list(result.per_url) == urls
        assert result.per_url[urls[1]].message.startswith("skipped")
        assert any(
            "event=fastly_wildcard_skipped" in r.getMessage() and urls[1] in r.getMessage()
            for r in caplog.records
        )

    @respx.mock
    async def test_all_terminal_is_not_retryable(self) -> None:
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(401)
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a", "https://repo.example.com/b"])
        assert result.ok is False
        assert result.retryable is False
        assert result.message == "2x HTTP 401"


SERVICE_DETAILS: dict[str, object] = {
    "id": "svc-id",
    "versions": [
        {"number": 1, "active": False},
        {"number": 42, "active": True},
    ],
}
DOMAINS_PAYLOAD: list[dict[str, str]] = [
    {"name": "repo.example.com"},
    {"name": "mirror.example.com"},
    {"name": "alt.example.com"},
]


class TestFastlyPurgeAllServiceDomains:
    """``purge_all_service_domains=True`` discovers service domains and
    expands each input URL across all of them."""

    @respx.mock
    async def test_expansion_purges_every_domain(self) -> None:
        respx.get("https://api.fastly.com/service/svc-id").mock(
            return_value=httpx.Response(200, json=SERVICE_DETAILS)
        )
        respx.get("https://api.fastly.com/service/svc-id/version/42/domain").mock(
            return_value=httpx.Response(200, json=DOMAINS_PAYLOAD)
        )
        # All purge endpoints succeed.
        purge_route = respx.post(url__regex=r"https://api\.fastly\.com/purge/[^/]+/foo\.rpm").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )

        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            result = await p.purge(["https://repo.example.com/foo.rpm"])

        assert result.ok is True
        # Three domains → three purge calls (the URL's own host is one of them
        # and is deduplicated, not double-purged).
        assert purge_route.call_count == 3
        hosts_purged = {call.request.url.path.split("/")[2] for call in purge_route.calls}
        assert hosts_purged == {
            "repo.example.com",
            "mirror.example.com",
            "alt.example.com",
        }

    @respx.mock
    async def test_variant_failure_reported_against_input_url(self) -> None:
        respx.get("https://api.fastly.com/service/svc-id").mock(
            return_value=httpx.Response(200, json=SERVICE_DETAILS)
        )
        respx.get("https://api.fastly.com/service/svc-id/version/42/domain").mock(
            return_value=httpx.Response(200, json=DOMAINS_PAYLOAD)
        )
        status_by_host = {
            "repo.example.com": 200,
            "mirror.example.com": 503,
            "alt.example.com": 400,
        }
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            side_effect=lambda request: httpx.Response(
                status_by_host[request.url.path.split("/")[2]]
            )
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            result = await p.purge(["https://repo.example.com/foo.rpm"])

        # Keyed by the job's URL, not the expanded variants, so a retry
        # re-expands it rather than piling variants into the job.
        assert result.per_url is not None
        assert list(result.per_url) == ["https://repo.example.com/foo.rpm"]
        outcome = result.per_url["https://repo.example.com/foo.rpm"]
        assert outcome.ok is False
        # One variant can still succeed on retry, so the terminal 400 on
        # another domain must not stop the retry.
        assert outcome.retryable is True
        assert outcome.message == "mirror.example.com: HTTP 503; alt.example.com: HTTP 400"

    @respx.mock
    async def test_shared_variants_purged_once(self) -> None:
        respx.get("https://api.fastly.com/service/svc-id").mock(
            return_value=httpx.Response(200, json=SERVICE_DETAILS)
        )
        respx.get("https://api.fastly.com/service/svc-id/version/42/domain").mock(
            return_value=httpx.Response(200, json=DOMAINS_PAYLOAD)
        )
        purge_route = respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            # Both inputs expand to the same three domain variants.
            result = await p.purge(
                ["https://repo.example.com/foo.rpm", "https://mirror.example.com/foo.rpm"]
            )
        assert result.ok is True
        assert purge_route.call_count == 3
        assert result.per_url is not None
        assert set(result.per_url) == {
            "https://repo.example.com/foo.rpm",
            "https://mirror.example.com/foo.rpm",
        }

    @respx.mock
    async def test_wildcard_only_batch_skips_discovery(self) -> None:
        # No routes at all: any discovery or purge call would fail the test.
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            result = await p.purge(["https://repo.example.com/almalinux/10/*"])
        assert result.ok is True

    @respx.mock
    async def test_discovery_cached_across_purges(self) -> None:
        details = respx.get("https://api.fastly.com/service/svc-id").mock(
            return_value=httpx.Response(200, json=SERVICE_DETAILS)
        )
        domains = respx.get("https://api.fastly.com/service/svc-id/version/42/domain").mock(
            return_value=httpx.Response(200, json=DOMAINS_PAYLOAD)
        )
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )

        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            await p.purge(["https://repo.example.com/a"])
            await p.purge(["https://repo.example.com/b"])

        # The two discovery calls happen once each, not per purge.
        assert details.call_count == 1
        assert domains.call_count == 1

    @respx.mock
    async def test_discovery_401_is_terminal(self) -> None:
        respx.get("https://api.fastly.com/service/svc-id").mock(return_value=httpx.Response(401))
        # No purge calls should be made.
        purge_route = respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is False
        assert result.retryable is False
        assert "discovery" in result.message.lower()
        assert purge_route.call_count == 0

    @respx.mock
    async def test_discovery_503_is_retryable(self) -> None:
        respx.get("https://api.fastly.com/service/svc-id").mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is False
        assert result.retryable is True

    @respx.mock
    async def test_no_active_version_is_terminal(self) -> None:
        respx.get("https://api.fastly.com/service/svc-id").mock(
            return_value=httpx.Response(
                200,
                json={"id": "svc-id", "versions": [{"number": 1, "active": False}]},
            )
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is False
        assert result.retryable is False
        assert "no active version" in result.message.lower()

    @respx.mock
    async def test_flag_off_skips_discovery(self) -> None:
        # No discovery routes registered; if the provider tried to call
        # them respx would raise. This confirms discovery is fully gated.
        purge_route = respx.post("https://api.fastly.com/purge/repo.example.com/a").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                # purge_all_service_domains defaults to False
            )
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is True
        assert purge_route.call_count == 1


# ---------------------------------------------------------------------------
# CloudFront
# ---------------------------------------------------------------------------


class TestCloudFrontProvider:
    async def test_urls_converted_to_paths(self) -> None:
        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.return_value = {
                "Invalidation": {"Id": "I123", "Status": "InProgress"}
            }
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="AKIA",
                secret_access_key="x",
            )
            result = await p.purge(
                [
                    "https://cdn-us.almalinux.org/almalinux/9/os/x86_64/repodata/repomd.xml",
                    "https://cdn-us.almalinux.org/almalinux/9/os/x86_64/Packages/x.rpm",
                ]
            )
        assert result.ok is True
        call = client.create_invalidation.call_args
        batch = call.kwargs["InvalidationBatch"]
        paths = batch["Paths"]["Items"]
        assert "/almalinux/9/os/x86_64/repodata/repomd.xml" in paths
        assert "/almalinux/9/os/x86_64/Packages/x.rpm" in paths

    async def test_wildcard_sent_as_prefix_path(self) -> None:
        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.return_value = {
                "Invalidation": {"Id": "I123", "Status": "InProgress"}
            }
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="x",
                secret_access_key="x",
            )
            result = await p.purge(
                ["https://cdn.example.com/almalinux/10", "https://cdn.example.com/almalinux/10/*"]
            )
        assert result.ok is True
        batch = client.create_invalidation.call_args.kwargs["InvalidationBatch"]
        assert batch["Paths"]["Items"] == ["/almalinux/10", "/almalinux/10/*"]

    async def test_throttling_is_retryable(self) -> None:
        from botocore.exceptions import ClientError

        err = ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
            "CreateInvalidation",
        )
        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = err
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="x",
                secret_access_key="x",
            )
            result = await p.purge(["https://cdn.example.com/a"])
        assert result.ok is False
        assert result.retryable is True
        # Only the in-progress limit is waited out inside purge().
        assert client.create_invalidation.call_count == 1

    async def test_in_progress_limit_waits_then_resubmits(self) -> None:
        from botocore.exceptions import ClientError

        limit = ClientError(
            {"Error": {"Code": "TooManyInvalidationsInProgress", "Message": "busy"}},
            "CreateInvalidation",
        )
        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = [
                limit,
                limit,
                {"Invalidation": {"Id": "I123", "Status": "InProgress"}},
            ]
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="x",
                secret_access_key="x",
                in_progress_poll_seconds=0.01,
                in_progress_max_wait_seconds=5.0,
            )
            result = await p.purge(["https://cdn.example.com/a", "https://cdn.example.com/b"])
        assert result.ok is True
        assert client.create_invalidation.call_count == 3
        # Every resubmit is the same invalidation, so CloudFront can never
        # create it twice.
        batches = [c.kwargs["InvalidationBatch"] for c in client.create_invalidation.call_args_list]
        assert all(b == batches[0] for b in batches)

    async def test_in_progress_limit_gives_up_after_max_wait(self) -> None:
        from botocore.exceptions import ClientError

        limit = ClientError(
            {"Error": {"Code": "TooManyInvalidationsInProgress", "Message": "busy"}},
            "CreateInvalidation",
        )
        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = limit
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="x",
                secret_access_key="x",
                in_progress_poll_seconds=0.01,
                in_progress_max_wait_seconds=0.05,
            )
            result = await p.purge(["https://cdn.example.com/a"])
        assert result.ok is False
        assert result.retryable is True
        assert result.message.startswith("TooManyInvalidationsInProgress: still at the limit")
        assert client.create_invalidation.call_count >= 3

    async def test_access_denied_is_terminal(self) -> None:
        from botocore.exceptions import ClientError

        err = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "nope"}},
            "CreateInvalidation",
        )
        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = err
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="x",
                secret_access_key="x",
            )
            result = await p.purge(["https://cdn.example.com/a"])
        assert result.ok is False
        assert result.retryable is False

    async def test_no_such_distribution_is_terminal(self) -> None:
        from botocore.exceptions import ClientError

        err = ClientError(
            {"Error": {"Code": "NoSuchDistribution", "Message": "not found"}},
            "CreateInvalidation",
        )
        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = err
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="x",
                secret_access_key="x",
            )
            result = await p.purge(["https://cdn.example.com/a"])
        assert result.retryable is False

    async def test_missing_credentials_is_terminal(self) -> None:
        # boto3 raises NoCredentialsError when its default credential chain
        # finds nothing. That can't be fixed by retrying.
        from botocore.exceptions import NoCredentialsError

        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = NoCredentialsError()
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id=None,
                secret_access_key=None,
            )
            result = await p.purge(["https://cdn.example.com/a"])
        assert result.ok is False
        assert result.retryable is False
        assert "auth" in result.message.lower()

    async def test_partial_credentials_is_terminal(self) -> None:
        # Half a credential pair (e.g. only AWS_ACCESS_KEY_ID in env) is
        # also terminal — operator needs to fix the config.
        from botocore.exceptions import PartialCredentialsError

        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = PartialCredentialsError(
                provider="env", cred_var="AWS_SECRET_ACCESS_KEY"
            )
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="AKIA",
                secret_access_key=None,
            )
            result = await p.purge(["https://cdn.example.com/a"])
        assert result.ok is False
        assert result.retryable is False


# ---------------------------------------------------------------------------
# GCP Cloud CDN
# ---------------------------------------------------------------------------


@pytest.fixture
def gcp_sa_file(tmp_path: Path) -> Path:
    """A bogus service-account JSON file. Tests patch the token-mint step."""
    sa = tmp_path / "gcp-sa.json"
    sa.write_text(
        '{"type": "service_account", "project_id": "alma-prod",'
        ' "private_key_id": "x", "private_key": "x", "client_email": "x@x.iam"}'
    )
    return sa


class TestGcpCdnProvider:
    @respx.mock
    async def test_one_call_per_url(self, gcp_sa_file: Path) -> None:
        route = respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(200, json={"status": "DONE"}))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                result = await p.purge(
                    [
                        "https://cdn-eu.almalinux.org/almalinux/9/x.rpm",
                        "https://cdn-eu.almalinux.org/almalinux/9/y.rpm",
                        "https://cdn-eu.almalinux.org/almalinux/9/z.rpm",
                    ]
                )
        assert result.ok is True
        assert route.call_count == 3

    @respx.mock
    async def test_request_body_has_path_and_host(self, gcp_sa_file: Path) -> None:
        route = respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(200, json={"status": "DONE"}))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                await p.purge(["https://cdn-eu.almalinux.org/almalinux/9/os/x86_64/x.rpm"])
        body = route.calls[0].request.read()
        import json

        payload = json.loads(body)
        assert payload["host"] == "cdn-eu.almalinux.org"
        assert payload["path"] == "/almalinux/9/os/x86_64/x.rpm"

    @respx.mock
    async def test_wildcard_sent_as_prefix_path(self, gcp_sa_file: Path) -> None:
        route = respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(200, json={"status": "DONE"}))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                result = await p.purge(["https://cdn-eu.almalinux.org/almalinux/10/*"])
        import json

        assert result.ok is True
        payload = json.loads(route.calls[0].request.read())
        assert payload == {"path": "/almalinux/10/*", "host": "cdn-eu.almalinux.org"}

    @respx.mock
    async def test_429_is_retryable(self, gcp_sa_file: Path) -> None:
        respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(429))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                result = await p.purge(["https://cdn-eu.almalinux.org/almalinux/9/x.rpm"])
        assert result.ok is False
        assert result.retryable is True

    @respx.mock
    async def test_quota_paces_calls(self, gcp_sa_file: Path) -> None:
        """With quota_per_minute=N, the (N+1)-th call must wait for the window.

        We set the quota low and time how long N+1 calls take. The (N+1)-th
        call should not return until the rate limiter has space; we monkey-
        patch the limiter's "minute" to a sub-second window so the test
        stays fast.
        """
        import asyncio

        respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(200, json={"status": "DONE"}))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                # The provider exposes the sliding-window length as
                # ``_quota_window_seconds`` for test injection. Production
                # uses 60.0.
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                    quota_per_minute=3,
                )
                p._quota_window_seconds = 0.5  # type: ignore[attr-defined]

                urls = [f"https://cdn.example/{i}" for i in range(4)]
                start = asyncio.get_event_loop().time()
                result = await p.purge(urls)
                elapsed = asyncio.get_event_loop().time() - start

        assert result.ok is True
        # First 3 should pace through immediately; the 4th must wait at
        # least one window expiry. We allow generous tolerance for jitter.
        assert elapsed >= 0.4, f"expected pacing to delay 4th call by ~window; elapsed={elapsed}"

    @respx.mock
    async def test_under_quota_does_not_pace(self, gcp_sa_file: Path) -> None:
        """With usage below quota, no artificial delay is introduced."""
        import asyncio

        respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(200, json={"status": "DONE"}))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                    quota_per_minute=500,
                )
                p._quota_window_seconds = 60.0  # type: ignore[attr-defined]
                start = asyncio.get_event_loop().time()
                await p.purge([f"https://cdn.example/{i}" for i in range(5)])
                elapsed = asyncio.get_event_loop().time() - start
        # 5 calls well under a 500/min quota — should be near-instant.
        assert elapsed < 0.5

    @respx.mock
    async def test_403_is_terminal(self, gcp_sa_file: Path) -> None:
        respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(403))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                result = await p.purge(["https://cdn-eu.almalinux.org/almalinux/9/x.rpm"])
        assert result.ok is False
        assert result.retryable is False

    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            pytest.param(
                {
                    "error": {
                        "code": 403,
                        "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}],
                    }
                },
                "rateLimitExceeded",
                id="legacy-rateLimitExceeded",
            ),
            pytest.param(
                {
                    "error": {
                        "code": 403,
                        "errors": [{"domain": "usageLimits", "reason": "userRateLimitExceeded"}],
                    }
                },
                "userRateLimitExceeded",
                id="legacy-userRateLimitExceeded",
            ),
            pytest.param(
                {
                    "error": {
                        "code": 403,
                        "status": "PERMISSION_DENIED",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                "reason": "RATE_LIMIT_EXCEEDED",
                            }
                        ],
                    }
                },
                "RATE_LIMIT_EXCEEDED",
                id="errorinfo-RATE_LIMIT_EXCEEDED",
            ),
            pytest.param(
                {"error": {"code": 403, "status": "RESOURCE_EXHAUSTED"}},
                "RESOURCE_EXHAUSTED",
                id="status-RESOURCE_EXHAUSTED",
            ),
        ],
    )
    @respx.mock
    async def test_rate_limit_403_is_retryable(
        self, gcp_sa_file: Path, body: dict[str, object], reason: str
    ) -> None:
        respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(403, json=body))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                result = await p.purge(["https://cdn-eu.almalinux.org/almalinux/9/x.rpm"])
        assert result.ok is False
        assert result.retryable is True
        assert reason in result.message

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param(
                httpx.Response(
                    403,
                    json={
                        "error": {
                            "code": 403,
                            "status": "PERMISSION_DENIED",
                            "errors": [{"domain": "global", "reason": "forbidden"}],
                        }
                    },
                ),
                id="permission-denied",
            ),
            pytest.param(httpx.Response(403, text="<html>Forbidden</html>"), id="non-json"),
            pytest.param(httpx.Response(403, json=["unexpected"]), id="non-object-json"),
        ],
    )
    @respx.mock
    async def test_non_rate_limit_403_is_terminal(
        self, gcp_sa_file: Path, response: httpx.Response
    ) -> None:
        respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=response)
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                result = await p.purge(["https://cdn-eu.almalinux.org/almalinux/9/x.rpm"])
        assert result.ok is False
        assert result.retryable is False

    @respx.mock
    async def test_mixed_results_reported_per_url(self, gcp_sa_file: Path) -> None:
        import json

        status_by_path = {"/ok": 200, "/gone": 404, "/busy": 429}
        respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(
            side_effect=lambda request: httpx.Response(
                status_by_path[json.loads(request.content)["path"]]
            )
        )
        urls = ["https://cdn.example/ok", "https://cdn.example/gone", "https://cdn.example/busy"]
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                result = await p.purge(urls)
        assert result.ok is False
        assert result.retryable is True
        assert result.per_url is not None
        ok, gone, busy = (result.per_url[u] for u in urls)
        assert ok.ok is True
        assert (gone.ok, gone.retryable) == (False, False)
        assert (busy.ok, busy.retryable) == (False, True)

    async def test_limiter_stamps_each_call_when_sent(self, gcp_sa_file: Path) -> None:
        """Quota slots are taken just before each request, not up front.

        With concurrency 1 and slow responses, requests go out one at a
        time. Each limiter timestamp must sit right before its request;
        stamping the whole batch at the start would put later stamps a
        full response-latency (or more) ahead of their requests.
        """
        import asyncio

        latency = 0.1
        loop = asyncio.get_running_loop()
        sent_at: list[float] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            sent_at.append(loop.time())
            await asyncio.sleep(latency)
            return httpx.Response(200, json={"status": "DONE"})

        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                    concurrency=1,
                )
                result = await p.purge([f"https://cdn.example/{i}" for i in range(3)])

        assert result.ok is True
        stamps = sorted(p._get_limiter()._calls)
        assert len(stamps) == len(sent_at) == 3
        gaps = [sent - stamp for stamp, sent in zip(stamps, sorted(sent_at), strict=True)]
        assert all(gap < latency / 2 for gap in gaps), f"stamp-to-send gaps: {gaps}"


# ---------------------------------------------------------------------------
# Dry-run provider
# ---------------------------------------------------------------------------


class TestDryRunProvider:
    """The dry-run provider must log every batch but never make an HTTP call."""

    @respx.mock
    async def test_purge_returns_success_without_calling_any_api(self) -> None:
        # No respx routes registered — any HTTP call would error.
        p = DryRunProvider(cdn_name="fastly_main", wrapped_type="fastly")
        result = await p.purge(["https://x.example/a", "https://x.example/b"])
        assert result.ok is True
        assert result.retryable is False
        assert "dry-run" in result.message.lower()

    async def test_purge_logs_each_url(self, caplog) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="mirror_mirage")
        p = DryRunProvider(cdn_name="cloudfront_us", wrapped_type="cloudfront")
        await p.purge(["https://cdn.example/a", "https://cdn.example/b"])
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        # Every URL should appear in the log output somewhere.
        assert "https://cdn.example/a" in log_text
        assert "https://cdn.example/b" in log_text
        # And the CDN name + wrapped type, so operators can attribute it.
        assert "cloudfront_us" in log_text
        assert "cloudfront" in log_text

    async def test_purge_handles_empty_batch(self) -> None:
        p = DryRunProvider(cdn_name="x", wrapped_type="fastly")
        result = await p.purge([])
        assert result.ok is True


class TestBuildProvidersDryRun:
    """build_providers(dry_run=True) must wrap every CDN with DryRunProvider."""

    def test_dry_run_wraps_every_cdn(self, basic_config) -> None:
        from mirror_mirage.providers import build_providers

        providers = build_providers(basic_config, dry_run=True)
        assert set(providers) == set(basic_config.cdns)
        for prov in providers.values():
            assert isinstance(prov, DryRunProvider)


# ---------------------------------------------------------------------------
# PurgeResult sanity
# ---------------------------------------------------------------------------


class TestPurgeResult:
    def test_success(self) -> None:
        r = PurgeResult(ok=True, retryable=False, message="done")
        assert r.ok and not r.retryable

    def test_retryable_failure(self) -> None:
        r = PurgeResult(ok=False, retryable=True, message="429")
        assert not r.ok and r.retryable

    def test_from_per_url_all_ok(self) -> None:
        ok = PurgeResult(ok=True, retryable=False, message="ok")
        r = PurgeResult.from_per_url({"a": ok, "b": ok})
        assert r.ok is True
        assert r.per_url == {"a": ok, "b": ok}

    def test_from_per_url_retryable_if_any_url_retryable(self) -> None:
        r = PurgeResult.from_per_url(
            {
                "a": PurgeResult(ok=True, retryable=False, message="ok"),
                "b": PurgeResult(ok=False, retryable=False, message="HTTP 404"),
                "c": PurgeResult(ok=False, retryable=True, message="HTTP 429"),
                "d": PurgeResult(ok=False, retryable=True, message="HTTP 429"),
            }
        )
        assert r.ok is False
        assert r.retryable is True
        assert r.message == "HTTP 404; 2x HTTP 429"

    def test_from_per_url_terminal_only(self) -> None:
        r = PurgeResult.from_per_url(
            {
                "a": PurgeResult(ok=True, retryable=False, message="ok"),
                "b": PurgeResult(ok=False, retryable=False, message="HTTP 404"),
            }
        )
        assert r.ok is False
        assert r.retryable is False
