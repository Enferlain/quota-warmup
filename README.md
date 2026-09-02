# Quota Warmup

Standalone Windows CLI for monitoring and cautiously warming quota windows for:

- OpenAI Codex, through the local `codex app-server` rate-limit RPC and `codex exec`.
- Google Antigravity, through `agy /usage` and `agy models`.
- GLM Coding Plan, through the custom Z.AI monitor endpoint and coding-plan chat API.

This is not a Codex plugin and does not use Hermes.

## Safety behavior

`status` and `run` are read-only by default. A model request requires `run --live`.
Task Scheduler registration is a separate explicit command.

The policy is:

1. The warmup target is specifically an unstarted `5h` model window. Weekly usage does not trigger a request.
2. A `5h` window is considered started when its reported usage is greater than zero or an exact activity signal shows usage despite a rounded `0%` display.
3. When the `5h` window is still at zero with no activity, the tool sends one request on the lowest configured model and effort for that model group.
4. The attempt is recorded before sending and held for five hours, so a tiny request that still displays as `0%`, an ambiguous timeout, or overlapping scheduler run cannot cause repeated requests.
5. Antigravity uses fractional usage, GLM uses its five-hour model-call/token feed, and Codex supplements rounded account percentages with local thread activity aligned to the provider's reset boundary.
6. Unknown quota status never causes an automatic live request, and a single-instance lock prevents overlapping live runs.

Antigravity currently reports two windows per model group: `weekly` and `5h`. The tool keeps those as separate state keys. It chooses the lowest configured available model and effort in each group: normally Gemini Flash Low for Gemini, and GPT-OSS Medium for the Claude/GPT group because that is the lowest advertised option in that group. If the installed CLI reports a different catalog, selection falls back to the lowest-ranked discovered model.

Codex’s app-server can expose primary and secondary rate-limit buckets when the account backend provides them. If Codex returns no rate limits, use the explicit `--force-provider codex` option for a manual warmup; automatic mode skips it.

## Setup

The Antigravity CLI is expected at `%LOCALAPPDATA%\agy\bin\agy.exe`; the tool also accepts `agy` on PATH. Codex is expected at `%LOCALAPPDATA%\pnpm\bin\codex.ps1`; the tool also accepts `codex` on PATH.

Create a local config only if you need overrides:

```powershell
Copy-Item .\config.example.json .\config.json
```

For GLM, expose the Agent Vault credential reference through one of the configured environment variables (`GLM_API_KEY`, `ZAI_API_KEY`, `ZAI_CODING_PLAN_API_KEY`, or `ZAI_API_TOKEN`). The real key is never written to the config, state, or log.

## Commands

From `C:\Users\Imi\quota-warmup`:

```powershell
python .\quota_warmup.py status
python .\quota_warmup.py status --provider antigravity --json
python .\quota_warmup.py run
python .\quota_warmup.py run --live
python .\quota_warmup.py run --force-provider codex --live
python .\quota_warmup.py logs --last 20
```

The first `run` is a dry-run. Every live scheduler check appends `runs.jsonl`, including no-op checks, unavailable providers, overlapping-run skips, and failures. `logs` renders the JSONL as a readable audit showing every quota value and why it warmed, skipped, or was ignored.

When ready to register scheduling explicitly:

```powershell
python .\quota_warmup.py install-task --name "Quota Warmup" --every-minutes 60
```

An hourly check is normally enough for a five-hour quota window and limits the delay after a reset to at most about one hour. A 15-minute schedule is also safe—the tool still checks quota first and its state hold prevents repeated warmup requests—but it performs four times as many read-only quota checks.

The scheduled task must run as the same Windows user that owns the Codex, Antigravity, and Agent Vault sessions. Agent Vault must be running so GLM requests can resolve the credential reference.

To remove that task:

```powershell
python .\quota_warmup.py remove-task --name "Quota Warmup"
```

## Verification

```powershell
python -m unittest -v
python .\quota_warmup.py status --provider antigravity
python .\quota_warmup.py run
```

The Antigravity status query uses the CLI’s non-interactive `/usage` report, which does not consume model tokens. Codex status uses the local app-server `account/rateLimits/read` RPC. GLM status uses the account’s monitor endpoint; only `run --live` sends a chat-completion request.

## References

- https://antigravity.google/docs/cli/commands/usage
- https://antigravity.google/docs/cli/headless
- https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
- https://docs.z.ai/devpack/overview
- https://docs.z.ai/api-reference/llm/chat-completion
- https://github.com/zai-org/zai-coding-plugins/blob/main/plugins/glm-plan-usage/README.md
