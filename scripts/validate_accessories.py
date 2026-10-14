#!/usr/bin/env python3
"""Validate a JSONL accessory dataset using the test endpoint."""

import argparse
import asyncio
import json
import math
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

import httpx


DEFAULT_ENDPOINT = "http://127.0.0.1:8000/api/v1/accessories/validate/test"


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def concurrency_value(value: str) -> int:
    number = positive_int(value)
    if number > 32:
        raise argparse.ArgumentTypeError("must be between 1 and 32")
    return number


def timeout_value(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Input JSONL file")
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Output JSONL file (appended by default)",
    )
    parser.add_argument(
        "--api-key", required=True, help="API key sent in llm_settings.api_key"
    )
    parser.add_argument(
        "--access-token", default=os.environ.get("ACCESSORY_VALIDATOR_ACCESS_TOKEN"),
        help="API bearer token (or set ACCESSORY_VALIDATOR_ACCESS_TOKEN)",
    )
    parser.add_argument(
        "--endpoint", default=DEFAULT_ENDPOINT, help="Full validate/test endpoint URL"
    )
    parser.add_argument(
        "--concurrency",
        type=concurrency_value,
        default=1,
        help="Concurrent requests, 1–32 (default: 1)",
    )
    parser.add_argument(
        "--from-record",
        type=positive_int,
        default=1,
        help="First record, inclusive and 1-based (default: 1)",
    )
    parser.add_argument(
        "--to-record",
        type=positive_int,
        help="Last record, inclusive and 1-based (default: end of file)",
    )
    parser.add_argument(
        "--timeout",
        type=timeout_value,
        default=1800,
        help="HTTP timeout in seconds (default: 1800)",
    )
    parser.add_argument(
        "--include-product-information",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include product information (enabled by default)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing output file instead of appending",
    )
    args = parser.parse_args(argv)
    if not args.api_key.strip():
        parser.error("--api-key must not be empty")
    if not args.access_token or not args.access_token.strip():
        parser.error("--access-token or ACCESSORY_VALIDATOR_ACCESS_TOKEN is required")
    args.access_token = args.access_token.strip()
    if args.to_record is not None and args.to_record < args.from_record:
        parser.error("--to-record must be greater than or equal to --from-record")
    for name in ("input", "output"):
        if getattr(args, name).suffix.lower() != ".jsonl":
            parser.error(f"--{name} must be a .jsonl file")
    if not args.input.is_file():
        parser.error("--input must be an existing file")
    if args.output.exists() and not args.output.is_file():
        parser.error("--output must be a file")
    if args.input.resolve() == args.output.resolve() or (
        args.output.exists() and args.input.samefile(args.output)
    ):
        parser.error("--input and --output must be different files")
    try:
        url = httpx.URL(args.endpoint)
    except httpx.InvalidURL as exc:
        parser.error(f"Invalid --endpoint: {exc}")
    if url.scheme not in ("http", "https") or not url.host:
        parser.error("--endpoint must be an HTTP or HTTPS URL")
    return args


def records(path: Path, first: int, last: int | None) -> Iterator[tuple[int, str]]:
    """Stream selected nonblank records without loading the dataset into memory."""
    with path.open(encoding="utf-8-sig") as source:
        record_number = 0
        for line in source:
            if not line.strip():
                continue
            record_number += 1
            if last is not None and record_number > last:
                break
            if record_number >= first:
                yield record_number, line


def redact(value: Any, api_key: str) -> Any:
    """Keep request keys out of saved input, responses, and errors."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower() in {"api_key", "authorization"}
            else redact(item, api_key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, api_key) for item in value]
    if isinstance(value, str):
        return value.replace(api_key, "[REDACTED]")
    return value


def redact_credentials(value: Any, args: argparse.Namespace) -> Any:
    return redact(redact(value, args.api_key), args.access_token)


def reject_constant(value: str) -> None:
    raise ValueError(f"Nonstandard JSON constant: {value}")


async def validate_record(
    client: httpx.AsyncClient, args: argparse.Namespace, number: int, line: str
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "record_number": number,
        "input": None,
        "status_code": None,
        "result": None,
        "error": None,
    }
    try:
        accessory = json.loads(line, parse_constant=reject_constant)
        # Exponent overflow (such as 1e999) also produces nonfinite floats.
        json.dumps(accessory, allow_nan=False)
        row["input"] = accessory
        if not isinstance(accessory, dict):
            raise ValueError("Each record must be a JSON object")
        settings = accessory.get("llm_settings", {})
        if not isinstance(settings, dict):
            raise ValueError("llm_settings must be a JSON object")
        payload = {
            **accessory,
            "llm_settings": {**settings, "api_key": args.api_key},
            "include_product_information": args.include_product_information,
        }
    except (ValueError, TypeError) as exc:
        row["error"] = {"type": "invalid_input", "message": str(exc)}
        return redact_credentials(row, args)

    try:
        response = await client.post(args.endpoint, json=payload)
    except httpx.RequestError as exc:
        row["error"] = {
            "type": "request_error",
            "message": f"{type(exc).__name__}: {exc}",
        }
        return redact_credentials(row, args)

    row["status_code"] = response.status_code
    try:
        body = json.loads(response.content, parse_constant=reject_constant)
        json.dumps(body, allow_nan=False)
    except ValueError:
        body = response.text
        if response.is_success:
            row["error"] = {
                "type": "invalid_response",
                "message": "Endpoint returned invalid JSON content",
                "details": body,
            }
            return redact_credentials(row, args)
    if response.is_success:
        row["result"] = body
    else:
        row["error"] = {
            "type": "http_error",
            "message": f"HTTP {response.status_code}",
            "details": body,
        }
    return redact_credentials(row, args)


def prepare_append(output: TextIO) -> None:
    """Avoid joining a new result to an existing JSONL record."""
    output.seek(0, os.SEEK_END)
    if output.tell() == 0:
        return
    output.seek(0)
    last_line = ""
    for line in output:
        last_line = line
    if not last_line.endswith("\n"):
        # A forced interruption may have left a partially written final record.
        # Preserve it for inspection instead of silently appending to corrupt JSON.
        try:
            json.loads(last_line, parse_constant=reject_constant)
        except ValueError as exc:
            raise ValueError(
                "Output ends with an incomplete JSON record; repair its last line "
                "or choose another output file before appending"
            ) from exc
        output.seek(0, os.SEEK_END)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    output.seek(0, os.SEEK_END)


async def run_batch(args: argparse.Namespace, output: TextIO) -> tuple[int, int]:
    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue(
        maxsize=args.concurrency
    )
    completed = 0
    errors = 0

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {args.access_token}"},
        timeout=args.timeout,
        limits=httpx.Limits(
            max_connections=args.concurrency, max_keepalive_connections=args.concurrency
        ),
    ) as client:

        async def worker() -> None:
            nonlocal completed, errors
            while True:
                item = await queue.get()
                if item is None:
                    return
                number, line = item
                row = await validate_record(client, args, number, line)
                # No await between receiving a result and persisting it. Each
                # worker writes a complete line before another worker can write.
                output.write(
                    json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
                )
                output.flush()
                os.fsync(output.fileno())
                completed += 1
                errors += row["error"] is not None
                state = "error" if row["error"] is not None else "saved"
                print(
                    f"Record {number}: {state} ({completed} completed, {errors} errors)",
                    file=sys.stderr,
                )

        async with asyncio.TaskGroup() as tasks:
            for _ in range(args.concurrency):
                tasks.create_task(worker())
            for item in records(args.input, args.from_record, args.to_record):
                await queue.put(item)
            for _ in range(args.concurrency):
                await queue.put(None)
    return completed, errors


def error_message(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(error_message(child) for child in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open(
            "w+" if args.overwrite else "a+", encoding="utf-8"
        ) as output:
            if not args.overwrite:
                prepare_append(output)
            completed, errors = asyncio.run(run_batch(args, output))
        print(
            f"Saved {completed} results to {args.output} ({errors} errors)",
            file=sys.stderr,
        )
        return 1 if errors else 0
    except KeyboardInterrupt:
        print(
            "Interrupted. Completed results have been saved to the output file.",
            file=sys.stderr,
        )
        return 130
    except (OSError, ValueError, ExceptionGroup) as exc:
        print(
            f"Batch stopped: {redact_credentials(error_message(exc), args)}. Completed results remain saved.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
