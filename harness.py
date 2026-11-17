#!/usr/bin/env python3
"""Minimal CLI harness for the Antigravity agent (Gemini Interactions API).

The harness is deliberately thin: it builds API requests, polls background
interactions until completion, prints model output and session identifiers,
and fetches text files produced in the remote sandbox.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_AGENT = "antigravity-preview-latest"
DEFAULT_MODEL = "gemini-4-hydrogen"
DEFAULT_POLL_SECONDS = 15
DEFAULT_TIMEOUT_SECONDS = 1800
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def get_api_key() -> str:
    """Return the API key from GEMINI_API_KEY or GOOGLE_API_KEY."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print(
            "Error: GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable is not set.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def derive_repo_name(repo_url: str) -> str:
    """Derive the repository directory name from a repository URL."""
    cleaned = repo_url.strip().rstrip("/")
    name = cleaned.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    if not name:
        raise ValueError(f"Could not derive repository name from URL: {repo_url!r}")
    return name


def build_environment(repo: str | None, env_id: str | None) -> str | dict[str, Any]:
    """Build the `environment` request field from CLI options."""
    if env_id:
        return env_id
    if repo:
        name = derive_repo_name(repo)
        return {
            "type": "remote",
            "sources": [
                {
                    "type": "repository",
                    "source": repo,
                    "target": f"/workspace/{name}",
                }
            ],
        }
    return "remote"


def build_run_body(
    task: str,
    agent: str,
    model: str,
    environment: str | dict[str, Any],
    previous: str | None = None,
) -> dict[str, Any]:
    """Build the request body for creating a `run` interaction."""
    body: dict[str, Any] = {
        "agent": agent,
        "input": task,
        "agent_config": {
            "type": "antigravity",
            "model": model,
        },
        "environment": environment,
        "background": True,
        "store": True,
    }
    if previous:
        body["previous_interaction_id"] = previous
    return body


def build_fetch_body(
    env_id: str,
    remote_path: str,
    agent: str,
    model: str,
) -> dict[str, Any]:
    """Build the request body for fetching a text file from an existing environment."""
    prompt = (
        f"Inspect the text file at {remote_path} in this environment.\n"
        f"First, run `wc -c` and `wc -l` on {remote_path} and output a single line "
        f"in the exact format:\n"
        f"FILESIZE <bytes> <lines>\n"
        f"Then print the file with `sed -n 'A,Bp'` in chunks of at most 400 lines. "
        f"Wrap each chunk between a line `<<<CHUNK_START>>>` and a line "
        f"`<<<CHUNK_END>>>`, verbatim, with no commentary inside or between chunks."
    )
    return {
        "agent": agent,
        "input": prompt,
        "agent_config": {
            "type": "antigravity",
            "model": model,
        },
        "environment_id": env_id,
        "background": True,
        "store": True,
    }


def api_request(
    method: str,
    url: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send an HTTP JSON request to the Gemini Interactions API."""
    headers = {
        "x-goog-api-key": api_key,
        "Accept": "application/json",
    }
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and "error" in parsed:
                err = parsed["error"]
                if isinstance(err, dict):
                    detail = err.get("message", body)
        except json.JSONDecodeError:
            pass
        print(f"API error ({exc.code} {exc.reason}): {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Network error: {exc.reason}", file=sys.stderr)
        sys.exit(1)


def poll_interaction(
    api_key: str,
    interaction_id: str,
    poll_interval: float,
    timeout: float,
    initial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Poll an interaction until it reaches a terminal status or times out."""
    start = time.monotonic()
    last_status: str | None = None

    current = initial
    if current is not None:
        status = str(current.get("status", "unknown"))
        print(f"[0.0s] status: {status}", file=sys.stderr)
        last_status = status
        if status in TERMINAL_STATUSES:
            return current

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            print(
                f"Timed out after {timeout:.0f}s waiting for interaction {interaction_id}. "
                f"You can check its status later with: python3 harness.py get {interaction_id}",
                file=sys.stderr,
            )
            sys.exit(1)

        sleep_for = min(poll_interval, max(0.0, timeout - elapsed))
        if sleep_for > 0:
            time.sleep(sleep_for)

        current = api_request("GET", f"{BASE_URL}/{interaction_id}", api_key)
        status = str(current.get("status", "unknown"))
        elapsed = time.monotonic() - start
        if status != last_status:
            print(f"[{elapsed:.1f}s] status: {status}", file=sys.stderr)
            last_status = status

        if status in TERMINAL_STATUSES:
            return current


def _extract_text_from_parts(parts: list[Any]) -> list[str]:
    texts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
    return texts


def extract_final_text(interaction: dict[str, Any]) -> str:
    """Concatenate text parts from steps whose type is 'model_output'."""
    chunks: list[str] = []
    steps = interaction.get("steps")
    if not isinstance(steps, list):
        return ""

    for step in steps:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        if isinstance(step.get("parts"), list):
            chunks.extend(_extract_text_from_parts(step["parts"]))
        elif isinstance(step.get("content"), str):
            chunks.append(step["content"])
        elif isinstance(step.get("content"), dict) and isinstance(
            step["content"].get("parts"), list
        ):
            chunks.extend(_extract_text_from_parts(step["content"]["parts"]))
        elif isinstance(step.get("content"), list):
            chunks.extend(_extract_text_from_parts(step["content"]))
        elif isinstance(step.get("text"), str):
            chunks.append(step["text"])

    return "".join(chunks)


def print_summary(interaction: dict[str, Any]) -> None:
    """Print interaction and environment identifiers to stderr."""
    interaction_id = interaction.get("id", "<unknown>")
    env_id = interaction.get("environment_id", "<unknown>")
    print(
        f"---\ninteraction_id: {interaction_id}\nenvironment_id: {env_id}",
        file=sys.stderr,
    )


def save_json(path: str, data: dict[str, Any]) -> None:
    """Write an interaction JSON object to disk."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def parse_fetched_output(text: str) -> tuple[int, int, str]:
    """Parse FILESIZE header and <<<CHUNK_START>>>...<<<CHUNK_END>>> blocks."""
    size_match = re.search(r"^FILESIZE\s+(\d+)\s+(\d+)\s*$", text, re.MULTILINE)
    if not size_match:
        raise ValueError("Agent output did not contain a valid `FILESIZE <bytes> <lines>` line.")

    reported_bytes = int(size_match.group(1))
    reported_lines = int(size_match.group(2))

    raw_chunks = re.findall(
        r"<<<CHUNK_START>>>\r?\n(.*?)<<<CHUNK_END>>>",
        text,
        re.DOTALL,
    )
    if not raw_chunks and (reported_bytes > 0 or reported_lines > 0):
        raise ValueError("Agent output did not contain any <<<CHUNK_START>>>...<<<CHUNK_END>>> blocks.")

    cleaned_chunks: list[str] = []
    for chunk in raw_chunks:
        normalized = chunk.replace("\r\n", "\n")
        if normalized.endswith("\n"):
            normalized = normalized[:-1]
        cleaned_chunks.append(normalized)

    if reported_bytes == 0 and reported_lines == 0 and not cleaned_chunks:
        content = ""
    elif reported_lines > 0:
        content = "\n".join(cleaned_chunks) + "\n"
    else:
        content = "\n".join(cleaned_chunks)

    return reported_bytes, reported_lines, content


def cmd_run(args: argparse.Namespace) -> int:
    task = " ".join(args.task)
    try:
        environment = build_environment(args.repo, args.env)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    body = build_run_body(
        task=task,
        agent=args.agent,
        model=args.model,
        environment=environment,
        previous=args.previous,
    )

    if args.dry_run:
        print(json.dumps(body, indent=2))
        return 0

    api_key = get_api_key()
    created = api_request("POST", BASE_URL, api_key, body)
    interaction_id = created.get("id")
    if not interaction_id:
        print("Error: API response did not include an interaction id.", file=sys.stderr)
        return 1

    final = poll_interaction(
        api_key=api_key,
        interaction_id=str(interaction_id),
        poll_interval=args.poll,
        timeout=args.timeout,
        initial=created,
    )

    if args.out:
        save_json(args.out, final)

    text = extract_final_text(final)
    if text:
        print(text)

    print_summary(final)
    return 0 if final.get("status") == "completed" else 1


def cmd_get(args: argparse.Namespace) -> int:
    api_key = get_api_key()
    interaction = api_request("GET", f"{BASE_URL}/{args.id}", api_key)

    if args.out:
        save_json(args.out, interaction)

    status = str(interaction.get("status", "unknown"))
    print(f"status: {status}")

    if status == "completed":
        text = extract_final_text(interaction)
        if text:
            print(text)

    print_summary(interaction)
    return 1 if status in {"failed", "cancelled"} else 0


def cmd_fetch(args: argparse.Namespace) -> int:
    api_key = get_api_key()
    body = build_fetch_body(
        env_id=args.env,
        remote_path=args.remote_path,
        agent=args.agent,
        model=args.model,
    )

    created = api_request("POST", BASE_URL, api_key, body)
    interaction_id = created.get("id")
    if not interaction_id:
        print("Error: API response did not include an interaction id.", file=sys.stderr)
        return 1

    final = poll_interaction(
        api_key=api_key,
        interaction_id=str(interaction_id),
        poll_interval=args.poll,
        timeout=args.timeout,
        initial=created,
    )

    if final.get("status") != "completed":
        print(
            f"Error: fetch interaction {interaction_id} ended with status {final.get('status')!r}.",
            file=sys.stderr,
        )
        print_summary(final)
        return 1

    text = extract_final_text(final)
    try:
        reported_bytes, reported_lines, content = parse_fetched_output(text)
    except ValueError as exc:
        print(f"Error parsing fetched output: {exc}", file=sys.stderr)
        print_summary(final)
        return 1

    written_lines = content.count("\n")
    if written_lines != reported_lines:
        print(
            f"Error: line count mismatch for {args.remote_path} "
            f"(reported {reported_lines} lines / {reported_bytes} bytes, got {written_lines} lines).",
            file=sys.stderr,
        )
        print_summary(final)
        return 1

    parent = os.path.dirname(os.path.abspath(args.to))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(args.to, "w", encoding="utf-8") as fh:
        fh.write(content)

    print(
        f"Fetched {args.remote_path} -> {args.to} ({written_lines} lines, {reported_bytes} reported bytes)",
        file=sys.stderr,
    )
    print_summary(final)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness.py",
        description="Minimal CLI harness for the Antigravity agent (Gemini Interactions API).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    run_parser = subparsers.add_parser("run", help="Run a task in a remote sandbox.")
    run_parser.add_argument("task", nargs="+", help="Task prompt words.")
    env_group = run_parser.add_mutually_exclusive_group()
    env_group.add_argument(
        "--repo",
        metavar="URL",
        help="Git repository URL to mount at /workspace/<name>.",
    )
    env_group.add_argument(
        "--env",
        metavar="ENV_ID",
        help="Existing environment ID to reuse.",
    )
    run_parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model name (default: {DEFAULT_MODEL}).",
    )
    run_parser.add_argument(
        "--agent",
        default=DEFAULT_AGENT,
        help=f"Agent name (default: {DEFAULT_AGENT}).",
    )
    run_parser.add_argument(
        "--previous",
        metavar="ID",
        help="Previous interaction ID to continue from (must be the latest in a chain).",
    )
    run_parser.add_argument(
        "--poll",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        metavar="N",
        help=f"Polling interval in seconds (default: {DEFAULT_POLL_SECONDS}).",
    )
    run_parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="N",
        help=f"Timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    run_parser.add_argument(
        "--out",
        metavar="FILE",
        help="Save the final interaction JSON to FILE.",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request body as JSON and exit without making network calls.",
    )
    run_parser.set_defaults(func=cmd_run)

    # get
    get_parser = subparsers.add_parser("get", help="Check status and output of an interaction.")
    get_parser.add_argument("id", metavar="ID", help="Interaction ID.")
    get_parser.add_argument(
        "--out",
        metavar="FILE",
        help="Save the interaction JSON to FILE.",
    )
    get_parser.set_defaults(func=cmd_get)

    # fetch
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch a text file from an existing sandbox environment.",
    )
    fetch_parser.add_argument(
        "--env",
        required=True,
        metavar="ENV_ID",
        help="Environment ID containing the file.",
    )
    fetch_parser.add_argument(
        "remote_path",
        metavar="REMOTE_PATH",
        help="Path to the text file inside the remote sandbox.",
    )
    fetch_parser.add_argument(
        "--to",
        required=True,
        metavar="LOCAL_PATH",
        help="Local destination file path.",
    )
    fetch_parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model name (default: {DEFAULT_MODEL}).",
    )
    fetch_parser.add_argument(
        "--agent",
        default=DEFAULT_AGENT,
        help=f"Agent name (default: {DEFAULT_AGENT}).",
    )
    fetch_parser.add_argument(
        "--poll",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        metavar="N",
        help=f"Polling interval in seconds (default: {DEFAULT_POLL_SECONDS}).",
    )
    fetch_parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="N",
        help=f"Timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    fetch_parser.set_defaults(func=cmd_fetch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
