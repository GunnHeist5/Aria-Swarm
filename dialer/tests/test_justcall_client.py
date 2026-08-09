"""JustCallClient: auth header shape, pagination, retry/backoff, throttle.

No network: the client's httpx.Client is swapped for one backed by
httpx.MockTransport, and its sleep function records instead of sleeping.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import make_settings

import orchestrator.justcall.client as client_mod
from orchestrator.config import ConfigError
from orchestrator.justcall.client import JustCallClient, JustCallError

API_KEY = "jc_key_0123456789"
API_SECRET = "jc_secret_9876543210"

FIXTURES = Path(__file__).parent / "fixtures"


def build_client(handler) -> tuple[JustCallClient, list[float]]:
    cfg = make_settings(justcall_api_key=API_KEY, justcall_api_secret=API_SECRET)
    client = JustCallClient(cfg)
    client._http = httpx.Client(
        base_url=client_mod.BASE_URL, transport=httpx.MockTransport(handler)
    )
    sleeps: list[float] = []
    client._sleep = sleeps.append
    return client, sleeps


def no_throttle(client: JustCallClient) -> None:
    client._bucket = type("NullBucket", (), {"reserve": staticmethod(lambda: 0.0)})()


def test_init_requires_credentials():
    with pytest.raises(ConfigError) as excinfo:
        JustCallClient(make_settings())
    assert "JUSTCALL_API_KEY" in str(excinfo.value)
    assert "JUSTCALL_API_SECRET" in str(excinfo.value)


def test_pagination_headers_and_params():
    requests: list[httpx.Request] = []
    pages = {0: [{"id": 1}, {"id": 2}], 1: [{"id": 3}]}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == f"{API_KEY}:{API_SECRET}"
        assert "Bearer" not in request.headers["Authorization"]
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(200, json={"data": pages[int(request.url.params["page"])]})

    client, _ = build_client(handler)
    got = list(
        client.fetch_campaign_contacts(
            progress_status="Undialed", contact_status="Active", per_page=2
        )
    )
    assert [c["id"] for c in got] == [1, 2, 3]
    # page is 0-based; page 1 was short (1 < per_page) so pagination stopped.
    assert [int(r.url.params["page"]) for r in requests] == [0, 1]
    first = requests[0].url.params
    assert first["campaign_id"] == "3310579"
    assert first["progress_status"] == "Undialed"
    assert first["contact_status"] == "Active"
    assert first["per_page"] == "2"


def test_pagination_stops_on_empty_page():
    requests: list[int] = []
    pages = {0: [{"id": 1}, {"id": 2}], 1: []}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        requests.append(page)
        return httpx.Response(200, json={"data": pages[page]})

    client, _ = build_client(handler)
    got = list(client.fetch_campaign_contacts(per_page=2))
    assert [c["id"] for c in got] == [1, 2]
    assert requests == [0, 1]


def test_fixture_pages_round_trip():
    pages = {
        0: json.loads((FIXTURES / "justcall_contacts_page0.json").read_text()),
        1: json.loads((FIXTURES / "justcall_contacts_page1.json").read_text()),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[int(request.url.params["page"])])

    client, _ = build_client(handler)
    got = list(client.fetch_campaign_contacts(per_page=3))
    assert len(got) == 5
    assert {"id", "name", "phone_number", "status", "created_at", "custom_fields"} <= set(got[0])
    assert got[0]["custom_fields"][0]["key"] == "company_name"


def test_tolerates_bare_list_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 9}])

    client, _ = build_client(handler)
    assert [c["id"] for c in client.fetch_campaign_contacts()] == [9]


def test_retry_429_respects_retry_after():
    count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        if count["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json={"data": []})

    client, sleeps = build_client(handler)
    no_throttle(client)
    assert list(client.fetch_campaign_contacts()) == []
    assert count["n"] == 2
    assert sleeps == [3.0]


def test_5xx_exhausts_retries_with_exponential_backoff():
    count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        return httpx.Response(500, text="upstream exploded")

    client, sleeps = build_client(handler)
    no_throttle(client)
    with pytest.raises(JustCallError) as excinfo:
        list(client.fetch_campaign_contacts())
    assert "500" in str(excinfo.value)
    assert count["n"] == 5  # 1 attempt + 4 retries
    assert sleeps == [0.5, 1.0, 2.0, 4.0]


def test_network_error_retries_then_succeeds():
    count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        if count["n"] == 1:
            raise httpx.ConnectError("kaboom")
        return httpx.Response(200, json={"data": []})

    client, _ = build_client(handler)
    no_throttle(client)
    assert list(client.fetch_campaign_contacts()) == []
    assert count["n"] == 2


def test_client_error_fails_immediately_without_secret_leak():
    count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        return httpx.Response(400, json={"message": "bad request"})

    client, sleeps = build_client(handler)
    no_throttle(client)
    with pytest.raises(JustCallError) as excinfo:
        list(client.fetch_campaign_contacts())
    assert count["n"] == 1
    assert sleeps == []
    message = str(excinfo.value)
    assert "400" in message
    assert API_KEY not in message
    assert API_SECRET not in message


def test_set_contact_dnca_request_shape():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"status": "ok"})

    client, _ = build_client(handler)
    client.set_contact_dnca(90210392)
    (request,) = captured
    assert request.method == "POST"
    assert request.url.path.endswith(client_mod.CONTACTS_PATH)
    body = json.loads(request.content)
    assert body["campaign_id"] == 3310579
    assert body["id"] == 90210392
    assert body["status"] == "DNCA"


def test_add_contact_note_request_shape():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"status": "ok"})

    client, _ = build_client(handler)
    client.add_contact_note(90210417, "AI call: interested, follow-up booked")
    (request,) = captured
    body = json.loads(request.content)
    assert body["id"] == 90210417
    assert body["notes"] == "AI call: interested, follow-up booked"


def test_token_bucket_math_with_fake_clock():
    now = [0.0]
    bucket = client_mod._TokenBucket(rate_per_sec=2.0, capacity=2.0, clock=lambda: now[0])
    assert bucket.reserve() == 0.0
    assert bucket.reserve() == 0.0
    # Bucket drained: the third caller must wait for one token at 2/s.
    assert bucket.reserve() == pytest.approx(0.5)
    now[0] += 10.0
    assert bucket.reserve() == 0.0  # fully refilled after idling


def test_throttle_engages_on_burst():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client, sleeps = build_client(handler)
    for _ in range(5):
        list(client.fetch_campaign_contacts())
    assert any(wait > 0 for wait in sleeps)
