"""Tests for :mod:`mirror_mirage.providers.fastly_dictionary`."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mirror_mirage.providers.fastly_dictionary import (
    BATCH_LIMIT,
    PAGE_SIZE,
    DictionaryError,
    FastlyDictionaryClient,
)

API = "https://api.fastly.com"
SERVICE = {"versions": [{"number": 6, "active": False}, {"number": 7, "active": True}]}
ITEMS_URL = f"{API}/service/svc/dictionary/dict1/items"


def _mock_resolve(*, body: dict[str, object] | None = None, status: int = 200) -> respx.Route:
    respx.get(f"{API}/service/svc").mock(return_value=httpx.Response(200, json=SERVICE))
    return respx.get(f"{API}/service/svc/version/7/dictionary/mirage_aliases").mock(
        return_value=httpx.Response(status, json=body or {"id": "dict1", "write_only": False})
    )


def _items(start: int, count: int) -> list[dict[str, str]]:
    return [{"item_key": f"/k{i:04d}", "item_value": f"/v{i}"} for i in range(start, start + count)]


async def _client(http: httpx.AsyncClient) -> FastlyDictionaryClient:
    return FastlyDictionaryClient(
        service_id="svc", api_token="tok", dictionary="mirage_aliases", client=http
    )


class TestItems:
    @respx.mock
    async def test_pages_until_short_page(self) -> None:
        _mock_resolve()
        pages = {1: _items(0, PAGE_SIZE), 2: _items(PAGE_SIZE, 3)}
        route = respx.get(ITEMS_URL).mock(
            side_effect=lambda req: httpx.Response(200, json=pages[int(req.url.params["page"])])
        )
        async with httpx.AsyncClient() as http:
            items = await (await _client(http)).items()
        assert len(items) == PAGE_SIZE + 3
        assert items["/k0002"] == "/v2"
        assert route.call_count == 2
        assert route.calls[0].request.headers["Fastly-Key"] == "tok"
        assert route.calls[0].request.url.params["per_page"] == str(PAGE_SIZE)

    @respx.mock
    async def test_stops_if_page_parameter_ignored(self) -> None:
        _mock_resolve()
        route = respx.get(ITEMS_URL).mock(
            return_value=httpx.Response(200, json=_items(0, PAGE_SIZE))
        )
        async with httpx.AsyncClient() as http:
            items = await (await _client(http)).items()
        assert len(items) == PAGE_SIZE
        assert route.call_count == 2

    @respx.mock
    async def test_dictionary_id_cached(self) -> None:
        resolve = _mock_resolve()
        respx.get(ITEMS_URL).mock(return_value=httpx.Response(200, json=[]))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            await client.items()
            await client.items()
        assert resolve.call_count == 1


class TestApply:
    @respx.mock
    async def test_batches_of_1000(self) -> None:
        _mock_resolve()
        route = respx.patch(ITEMS_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))
        ops = [{"op": "upsert", "item_key": f"/k{i}", "item_value": "/v"} for i in range(1500)]
        async with httpx.AsyncClient() as http:
            await (await _client(http)).apply(ops)
        sizes = [len(json.loads(c.request.content)["items"]) for c in route.calls]
        assert sizes == [BATCH_LIMIT, 500]

    @respx.mock
    async def test_empty_ops_make_no_calls(self) -> None:
        async with httpx.AsyncClient() as http:
            await (await _client(http)).apply([])
        assert respx.calls.call_count == 0


class TestErrors:
    @respx.mock
    async def test_missing_dictionary_is_terminal(self) -> None:
        _mock_resolve(status=404, body={"msg": "Record not found"})
        async with httpx.AsyncClient() as http:
            with pytest.raises(DictionaryError, match="not found on active version 7") as exc:
                await (await _client(http)).items()
        assert exc.value.retryable is False

    @respx.mock
    async def test_write_only_dictionary_rejected(self) -> None:
        _mock_resolve(body={"id": "dict1", "write_only": True})
        async with httpx.AsyncClient() as http:
            with pytest.raises(DictionaryError, match="write-only"):
                await (await _client(http)).items()

    @respx.mock
    async def test_no_active_version(self) -> None:
        respx.get(f"{API}/service/svc").mock(
            return_value=httpx.Response(200, json={"versions": [{"number": 1, "active": False}]})
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(DictionaryError, match="no active version"):
                await (await _client(http)).items()

    @pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (401, False)])
    @respx.mock
    async def test_status_classification(self, status: int, retryable: bool) -> None:
        _mock_resolve()
        respx.patch(ITEMS_URL).mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as http:
            with pytest.raises(DictionaryError) as exc:
                await (await _client(http)).apply([{"op": "delete", "item_key": "/k"}])
        assert exc.value.retryable is retryable

    @respx.mock
    async def test_transport_error_retryable(self) -> None:
        respx.get(f"{API}/service/svc").mock(side_effect=httpx.ConnectError("down"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(DictionaryError) as exc:
                await (await _client(http)).items()
        assert exc.value.retryable is True

    @respx.mock
    async def test_404_on_items_forgets_cached_id(self) -> None:
        resolve = _mock_resolve()
        respx.get(ITEMS_URL).mock(
            side_effect=[
                httpx.Response(200, json=[]),
                httpx.Response(404),
                httpx.Response(200, json=[]),
            ]
        )
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            await client.items()
            with pytest.raises(DictionaryError):
                await client.items()
            await client.items()
        assert resolve.call_count == 2
