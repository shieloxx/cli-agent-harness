# cli-agent-harness

Minimal CLI harness for an AI coding agent powered by the Antigravity agent (Gemini Interactions API).

## Philosophy

`cli-agent-harness` is deliberately thin. The Antigravity agent running in its remote sandbox does all the thinking, planning, coding, and testing. The harness does **not** implement an agent loop, planner, prompt engineering framework, or local tool execution — it only moves requests, time, and files:

1. Builds a Gemini Interactions API request from CLI flags.
2. Runs the interaction in background mode and polls until completion.
3. Prints the agent's final output and session identifiers.
4. Fetches text files produced in the remote sandbox back to your local machine.

## Setup

- **Python**: 3.10 or newer (standard library only, no `pip install` required).
- **API Key**: Export `GEMINI_API_KEY` (or `GOOGLE_API_KEY` as a fallback):

```bash
export GEMINI_API_KEY="your-api-key"
```

## Usage

### Run a task in a fresh sandbox

```bash
python3 harness.py run "Write a Python script that parses CSV files and add unit tests"
```

### Run a task against a GitHub repository

```bash
python3 harness.py run "Fix the failing tests" --repo https://github.com/shieloxx/cli-agent-harness
```

### Continue from a previous interaction

```bash
python3 harness.py run "Now add a CLI flag for verbose logging" --previous int_abc123
```

### Inspect the request payload without calling the API (`--dry-run`)

```bash
python3 harness.py run "hello" --dry-run
```

### Check an existing interaction

```bash
python3 harness.py get int_abc123 --out interaction.json
```

### Fetch a text file from a sandbox environment

```bash
python3 harness.py fetch --env env_abc123 /workspace/cli-agent-harness/harness.py --to ./harness.py
```

## API parameters

The harness targets `https://generativelanguage.googleapis.com/v1beta/interactions` using `x-goog-api-key` authentication.

- **Agent & model**: Defaults to `agent: "antigravity-preview-latest"` and `agent_config: {"type": "antigravity", "model": "gemini-4-hydrogen"}`. Override with `--agent` and `--model`.
- **`background` and `store`**: Always set to `background: true` and `store: true` so long-running coding tasks execute asynchronously and remain retrievable via `get` or `fetch`.
- **`environment` forms** (for `run`):
  - Fresh sandbox (default): `"remote"`
  - Repository source (`--repo <URL>`):
    ```json
    {
      "type": "remote",
      "sources": [
        {
          "type": "repository",
          "source": "https://github.com/owner/repo",
          "target": "/workspace/repo"
        }
      ]
    }
    ```
    The `<name>` in `/workspace/<name>` is derived from the repository URL basename with any trailing `.git` stripped.
  - Existing environment (`--env <ENV_ID>`): passes the environment ID string (e.g., `"env_abc123"`) directly as `environment`.
- **`environment_id`** (for `fetch`): `fetch` starts a new interaction in an existing sandbox using the `environment_id` body field instead of `environment`.
- **Continuation (`--previous <ID>`)**: Sets `previous_interaction_id` in the request body. Only the **latest** interaction in a chain can be continued.

## Limitations

- **`fetch` is text-only**: `fetch` transfers UTF-8 text files via chunked `sed` output and verifies line counts against `wc -l`. It cannot transfer binary files.
- **No GitHub credentials in the sandbox**: The remote sandbox does not hold your GitHub credentials and cannot `git push`. Use `fetch` to retrieve modified or newly created files back to your local machine.
- **Preview API**: The Gemini Interactions API and Antigravity agent are in preview; request/response schemas and model names may change.
- **Token costs**: Usage costs follow the underlying model's input and output token pricing.
