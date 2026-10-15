import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.llm import LiteLLMClient, LiteLLMConfig, RecordingLLMClient
from app.main import app

SERVER_KEY = "server-secret-key"
CALLER_KEY = "caller-key"

PAYLOAD = {
    "brand": "Example",
    "model": "Rack",
    "price": "49.99",
    "origin": {"source": "odoo", "external_ref": "ACC-42"},
}


def llm_response(text, *, with_web_search=False):
    output = []
    annotations = []
    if with_web_search:
        output.append({"type": "web_search_call", "id": "ws", "status": "completed"})
        annotations.append(
            {
                "type": "url_citation",
                "url": "https://manufacturer.example/rack",
                "title": "Rear rack",
            }
        )
    output.append(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": annotations}],
        }
    )
    return {"object": "response", "status": "completed", "model": "m", "output": output}


@pytest.fixture
def api(auth_context, monkeypatch):
    """App state wired like production, but without a database or real LLM."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        instructions = body.get("instructions", "")
        if body.get("tools"):
            text = "A rear rack."
        elif 'Is this accessory leasable according to the matched rule?' in instructions:
            text = '{"answer": "UNKNOWN", "details": "No special rule matched."}'
        elif "Decide whether the submitted product clearly matches" in instructions:
            text = '{"answer": "NO", "details": "Not excluded."}'
        else:
            text = '{"answer": "YES", "details": "Leasable."}'
        return httpx.Response(
            200, json=llm_response(text, with_web_search=bool(body.get("tools")))
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    server_config = LiteLLMConfig(
        base_url="https://server-gateway.example/v1",
        api_key=SERVER_KEY,
        models=("server-model",),
        temperature=0.7,
    )
    report_repository = AsyncMock()
    llm_request_repository = AsyncMock()
    app.state.http_client = http_client
    app.state.validation_report_repository = report_repository
    app.state.litellm_client = RecordingLLMClient(
        LiteLLMClient(http_client, server_config), llm_request_repository
    )
    # No lifespan: the fixture provides the state that startup would create.
    monkeypatch.setattr(app.state, "jwt_verifier", auth_context.verifier, raising=False)
    client = TestClient(app, headers={"Authorization": f"Bearer {auth_context.mint()}"})
    yield client, requests, report_repository, llm_request_repository


def post(client, **extra):
    return client.post("/api/v1/accessories/validate/test", json={**PAYLOAD, **extra})


def test_requires_api_key(api):
    client, requests, *_ = api

    assert post(client).status_code == 422
    assert post(client, llm_settings={}).status_code == 422
    assert post(client, llm_settings={"api_key": ""}).status_code == 422
    assert requests == []


def test_never_uses_server_api_key_and_stores_nothing(api):
    client, requests, report_repository, llm_request_repository = api

    response = post(client, llm_settings={"api_key": CALLER_KEY})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "VALID"
    assert requests
    for sent in requests:
        assert sent.headers["Authorization"] == f"Bearer {CALLER_KEY}"
        assert SERVER_KEY not in sent.headers["Authorization"]
        assert sent.url.host == "server-gateway.example"
    report_repository.save.assert_not_awaited()
    llm_request_repository.save.assert_not_awaited()


def test_general_settings_override_server_defaults(api):
    client, requests, *_ = api

    response = post(
        client,
        llm_settings={
            "api_key": CALLER_KEY,
            "base_url": "https://caller-gateway.example/v1",
            "models": ["caller-model"],
            "temperature": 0.1,
            "max_tokens": 300,
            "reasoning_effort": "low",
        },
    )

    assert response.status_code == 200, response.text
    information_request = json.loads(requests[0].content)
    assert requests[0].url == "https://caller-gateway.example/v1/responses"
    assert information_request["model"] == "caller-model"
    assert information_request["temperature"] == 0.1
    assert information_request["max_output_tokens"] == 300
    assert information_request["reasoning"] == {"effort": "low"}
    # Criteria use their default models and reasoning, but inherit temperature.
    criterion_request = json.loads(requests[1].content)
    assert criterion_request["model"] == "gpt-6-luna"
    assert criterion_request["temperature"] == 0.1
    assert criterion_request["reasoning"] == {"effort": "xhigh"}


def test_criterion_settings_apply_only_to_their_criterion(api):
    client, requests, *_ = api

    response = post(
        client,
        llm_settings={"api_key": CALLER_KEY, "temperature": 0.3},
        criterion_settings={
            "explicitly_leasable_type": {"models": ["special"], "temperature": 0}
        },
    )

    assert response.status_code == 200, response.text
    not_leasable, leasable = (json.loads(r.content) for r in requests[1:3])
    assert not_leasable["model"] == "openai/gpt-6-luna"
    assert not_leasable["temperature"] == 0.3
    assert leasable["model"] == "special"
    assert leasable["temperature"] == 0
    for sent in requests:
        assert sent.headers["Authorization"] == f"Bearer {CALLER_KEY}"


@pytest.mark.parametrize(
    "criterion_settings",
    [
        {"unknown_criterion": {}},
        {"special_rules": {"api_key": "other"}},
        {"special_rules": {"base_url": "https://other.example/v1"}},
        {"special_rules": {"models": []}},
        {"special_rules": {"temperature": 3}},
        {"special_rules": {"reasoning_effort": "extreme"}},
    ],
)
def test_rejects_invalid_criterion_settings(api, criterion_settings):
    client, requests, *_ = api

    response = post(
        client,
        llm_settings={"api_key": CALLER_KEY},
        criterion_settings=criterion_settings,
    )

    assert response.status_code == 422
    assert requests == []


@pytest.mark.parametrize("options", [{}, {"include_product_information": False}])
def test_product_information_is_omitted_by_default_or_when_disabled(api, options):
    client, *_ = api

    response = post(client, llm_settings={"api_key": CALLER_KEY}, **options)

    assert response.status_code == 200, response.text
    assert "product_information" not in response.json()


@pytest.mark.parametrize("is_bawu", [False, True])
def test_includes_product_information_without_additional_requests_or_storage(
    api, is_bawu
):
    client, requests, report_repository, llm_request_repository = api

    response = post(
        client,
        llm_settings={"api_key": CALLER_KEY},
        include_product_information=True,
        context={"is_bawu_order": is_bawu},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["product_information"] == {
        "summary": "A rear rack.",
        "model": "m",
        "used_web_search": True,
        "sources": [{"url": "https://manufacturer.example/rack", "title": "Rear rack"}],
    }
    information_request = json.loads(requests[0].content)
    assert information_request["tools"] == [{"type": "web_search"}]
    assert information_request["tool_choice"] == "required"
    assert sum(bool(json.loads(sent.content).get("tools")) for sent in requests) == 1
    assert len(requests) == 1 + len(body["validations"][0]["criterion_results"])
    report_repository.save.assert_not_awaited()
    llm_request_repository.save.assert_not_awaited()


def test_regular_validation_does_not_expose_product_information(api):
    client, _, report_repository, _ = api

    response = client.post("/api/v1/accessories/validate", json=PAYLOAD)

    assert response.status_code == 200, response.text
    assert "product_information" not in response.json()
    report_repository.save.assert_awaited_once()


@pytest.mark.parametrize("status", [400, 404, 429])
def test_provider_errors_return_safe_details_and_log_them(api, monkeypatch, caplog, status):
    client, _, report_repository, llm_request_repository = api
    provider_response = httpx.Response(status, json={"error": {
        "message": f"Unsupported parameter 'temperature'. API key: {CALLER_KEY}",
        "param": "temperature",
        "code": "unsupported_parameter",
        "private_debug": "private provider debug data",
    }})
    monkeypatch.setattr(
        app.state.http_client, "post", AsyncMock(return_value=provider_response)
    )

    response = post(client, llm_settings={"api_key": CALLER_KEY})

    assert response.status_code == 503, response.text
    error = response.json()["errors"][0]
    assert error["code"] == "VALIDATION_EXECUTION_ERROR"
    assert f"HTTP {status}" in error["details"]
    assert "model 'server-model'" in error["details"]
    assert "during 'product_information'" in error["details"]
    assert "Unsupported parameter 'temperature'" in error["details"]
    assert "param: temperature" in error["details"]
    assert "code: unsupported_parameter" in error["details"]
    for text in (response.text, caplog.text):
        assert CALLER_KEY not in text
        assert "private provider debug data" not in text
    assert "Validation accessory_leasability failed: LLMError:" in caplog.text
    assert "Unsupported parameter 'temperature'" in caplog.text
    report_repository.save.assert_not_awaited()
    llm_request_repository.save.assert_not_awaited()


def test_unexpected_failures_are_internal_errors_with_logged_cause(
    api, monkeypatch, caplog
):
    client, _, report_repository, _ = api
    monkeypatch.setattr(
        app.state.http_client,
        "post",
        AsyncMock(side_effect=RuntimeError("internal detail")),
    )

    response = post(client, llm_settings={"api_key": CALLER_KEY})

    assert response.status_code == 500, response.text
    error = response.json()["errors"][0]
    assert error["code"] == "INTERNAL_ERROR"
    assert "internal detail" not in response.text
    assert "RuntimeError: internal detail" in caplog.text
    assert "Traceback" in caplog.text
    report_repository.save.assert_not_awaited()


def test_api_key_is_not_echoed_in_validation_errors(api):
    client, *_ = api

    response = post(
        client, llm_settings={"api_key": CALLER_KEY, "temperature": "hot"}
    )

    assert response.status_code == 422
    assert CALLER_KEY not in response.text


@pytest.mark.parametrize("is_bawu", [False, True])
@pytest.mark.parametrize(
    "answer,status", [("YES", "VALID"), ("NO", "INVALID"), ("UNKNOWN", "VALID")]
)
def test_special_rule_eligibility_answer_controls_strategy(
    api, monkeypatch, is_bawu, answer, status
):
    client, _, report_repository, llm_request_repository = api
    responses = [
        httpx.Response(
            200, json=llm_response("An e-bike battery.", with_web_search=True)
        ),
        *[
            httpx.Response(200, json=llm_response(json.dumps(criterion_answer)))
            for criterion_answer in [
                {"answer": "NO", "details": "No explicit exclusion."},
                {"answer": "NO", "details": "No explicit approval."},
                {"answer": answer, "details": "Battery special-rule eligibility."},
                {"answer": "YES", "details": "Technical component."},
            ]
        ],
    ]
    generate = AsyncMock(side_effect=responses)
    monkeypatch.setattr(app.state.http_client, "post", generate)

    response = post(
        client, brand="Bosch", model="600wh",
        context={"is_bawu_order": is_bawu}, llm_settings={"api_key": CALLER_KEY}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == status
    results = response.json()["validations"][0]["criterion_results"]
    assert results[2] == {
        "criterion_id": "special_rules",
        "answer": answer,
        "details": "Battery special-rule eligibility.",
    }
    assert generate.await_count == (5 if answer == "UNKNOWN" else 4)
    if answer == "UNKNOWN":
        assert results[-1]["criterion_id"] == "technical_bicycle_component"
    report_repository.save.assert_not_awaited()
    llm_request_repository.save.assert_not_awaited()
