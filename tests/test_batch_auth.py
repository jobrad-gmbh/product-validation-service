import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from scripts import validate_accessories as batch


def test_batch_sends_api_token_separately_and_redacts_both_credentials(
    tmp_path, monkeypatch
):
    source = tmp_path / "input.jsonl"
    source.write_text('{"brand":"Example","model":"Rack"}\n')
    args = SimpleNamespace(
        input=source,
        from_record=1,
        to_record=None,
        concurrency=1,
        timeout=5,
        api_key="llm-secret",
        access_token="api-secret",
        include_product_information=True,
        endpoint="https://validator.example/api/v1/accessories/validate/test",
    )
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"echo": "llm-secret api-secret"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        batch.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    output = tmp_path / "result.jsonl"
    with output.open("w+") as stream:
        assert asyncio.run(batch.run_batch(args, stream)) == (1, 0)
    assert seen[0].headers["Authorization"] == "Bearer api-secret"
    assert json.loads(seen[0].content)["llm_settings"]["api_key"] == "llm-secret"
    saved = output.read_text()
    assert "api-secret" not in saved
    assert "llm-secret" not in saved
    assert json.loads(saved)["result"]["echo"] == "[REDACTED] [REDACTED]"


def test_api_token_is_required_before_opening_output(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESSORY_VALIDATOR_ACCESS_TOKEN", raising=False)
    source = tmp_path / "input.jsonl"
    source.write_text("{}\n")
    output = tmp_path / "output.jsonl"
    with pytest.raises(SystemExit) as error:
        batch.parse_args(
            ["--input", str(source), "--output", str(output), "--api-key", "llm-key"]
        )
    assert error.value.code == 2
    assert not output.exists()


def test_api_token_can_come_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESSORY_VALIDATOR_ACCESS_TOKEN", "env-api-token")
    source = tmp_path / "input.jsonl"
    source.write_text("{}\n")
    args = batch.parse_args(
        [
            "--input",
            str(source),
            "--output",
            str(tmp_path / "output.jsonl"),
            "--api-key",
            "llm-key",
        ]
    )
    assert args.access_token == "env-api-token"


def test_network_errors_cannot_echo_api_token():
    async def run():
        def respond(request):
            raise httpx.ConnectError("api-secret llm-secret", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await batch.validate_record(
                client,
                SimpleNamespace(
                    access_token="api-secret",
                    api_key="llm-secret",
                    include_product_information=True,
                    endpoint="https://validator.example/test",
                ),
                1,
                "{}",
            )

    output = json.dumps(asyncio.run(run()))
    assert "api-secret" not in output and "llm-secret" not in output
