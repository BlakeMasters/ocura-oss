# SPDX-License-Identifier: MPL-2.0
"""Fixed Codex CLI adapter; all prompts, responses and usage belong to the ledger."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from rsi_io import bounded_process, checked_json, write_json


def response_schema(field):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {field: {"type": "string"}, "rationale": {"type": "string"}},
        "required": [field, "rationale"],
    }


def execute(request, schema_path):
    executable = shutil.which("codex")
    if executable is None:
        raise ValueError("Codex CLI not found; install and sign in before live experiments")
    command = [
        executable,
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "--color",
        "never",
        "--model",
        request["model"],
        "--output-schema",
        str(schema_path),
    ]
    if request.get("effort"):
        command.extend(["-c", 'model_reasoning_effort="' + request["effort"] + '"'])
    command.append("-")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ocura-rsi-agent-") as workspace:
        code, out, err = bounded_process(
            command, request["timeout"], input_bytes=request["prompt"].encode(), cwd=workspace
        )
    sys.stderr.buffer.write(err)

    # Raw events stay inside the verified response. No parsing of a separate unverified file.
    def reject_constant(value):
        raise ValueError(f"nonfinite event JSON constant: {value}")

    events, malformed = [], []
    for line in out.decode(errors="replace").splitlines():
        if line.strip():
            try:
                events.append(json.loads(line, parse_constant=reject_constant))
            except ValueError:
                malformed.append(line)
    usage, messages, forbidden, event_errors = [], [], [], []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            event_errors.append(f"event {index} must be an object with a string type")
            continue
        if event["type"] == "turn.completed":
            counts = event.get("usage")
            if (
                not isinstance(counts, dict)
                or not {"input_tokens", "output_tokens"} <= counts.keys()
                or any(type(value) is not int or value < 0 for value in counts.values())
            ):
                event_errors.append(f"event {index} has invalid token usage")
            else:
                usage.append(counts)
        elif event["type"] == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                event_errors.append(f"event {index} has an invalid completed item")
            elif item["type"] == "agent_message":
                if isinstance(item.get("text"), str):
                    messages.append(item["text"])
                else:
                    event_errors.append(f"event {index} has non-string message text")
            elif item["type"] != "reasoning":
                forbidden.append(item["type"])
        elif event["type"] in ("error", "turn.failed"):
            event_errors.append(f"event {index} reports {event['type']}")
    response = None
    error = None
    try:
        if code != 0 or not messages or forbidden or malformed or event_errors:
            raise ValueError(
                f"agent failed: exit={code}, tool_items={forbidden}, "
                f"malformed_lines={len(malformed)}, event_errors={event_errors}"
            )
        parsed = json.loads(messages[-1], parse_constant=reject_constant)
        field = request["field"]
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {field, "rationale"}
            or not all(isinstance(parsed[k], str) and len(parsed[k]) <= 12000 for k in parsed)
        ):
            raise ValueError("agent response violates the source/rationale contract")
        response = parsed
    except (ValueError, TypeError) as exc:
        error = str(exc)
    return {
        "ok": error is None,
        "error": error,
        "response": response,
        "usage": usage,
        "seconds": time.monotonic() - started,
        "model": request["model"],
        "effort": request.get("effort"),
        "events": events,
        "unparsed_stdout": malformed,
        "event_errors": event_errors,
        "exit_code": code,
        "workspace": "empty-temporary-directory",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    request = checked_json(args.request, args.sha256)
    schema = args.request.with_suffix(".schema.json")
    write_json(schema, response_schema(request["field"]))
    result = execute(request, schema)
    print(json.dumps(result, allow_nan=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
