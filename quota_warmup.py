#!/usr/bin/env python3
"""Quota-window warmup for Codex, Antigravity, and GLM.

The default command is intentionally read-only. A live model request requires
the explicit ``run --live`` flag, and Task Scheduler registration is a separate
explicit command.
"""

from __future__ import annotations

import argparse
import json
import msvcrt
import os
import queue
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


VERSION = "0.3.1"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"
DEFAULT_STATE = PROJECT_DIR / "state.json"
DEFAULT_LOG = PROJECT_DIR / "runs.jsonl"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def hidden_subprocess_options() -> dict[str, Any]:
    options: dict[str, Any] = {"creationflags": NO_WINDOW}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startupinfo
    return options


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def clamp_fraction(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def as_fraction(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1:
        number /= 100.0
    return clamp_fraction(number)


def parse_json_blob(text: str) -> Any:
    """Parse JSON even when a CLI adds a short diagnostic prefix/suffix."""
    text = text.strip()
    if not text:
        raise ValueError("empty JSON output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
        if not starts:
            raise
        start = min(starts)
        decoder = json.JSONDecoder()
        value, _ = decoder.raw_decode(text[start:])
        return value


def deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), Mapping):
            deep_merge(base[key], value)  # type: ignore[index]
        else:
            base[key] = value
    return base


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def save_json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def executable_parts(value: str | Sequence[str] | None, fallback: str) -> list[str]:
    if value is None or value == "":
        parts = [fallback]
    elif isinstance(value, str):
        path_value = Path(value)
        parts = [value] if path_value.exists() or path_value.suffix.lower() == ".ps1" else shlex.split(value, posix=False)
    else:
        parts = list(value)
    if not parts:
        parts = [fallback]
    if Path(parts[0]).suffix.lower() == ".ps1":
        parts = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *parts]
    return parts


def run_command(
    command: Sequence[str],
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=dict(environment) if environment is not None else None,
            **hidden_subprocess_options(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Executable not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {timeout:g}s: {command[0]}") from exc


def http_json(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
) -> tuple[int, Any]:
    data = None
    request_headers = dict(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, parse_json_blob(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed: Any = parse_json_blob(body) if body else {}
        except (ValueError, json.JSONDecodeError):
            parsed = {"raw": body[:1000]}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise RuntimeError(f"HTTP request failed for {url}: {exc.reason}") from exc


@dataclass
class Quota:
    provider: str
    quota_id: str
    group: str
    window: str
    remaining_fraction: float | None
    reset_time: str | None = None
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def used_fraction(self) -> float | None:
        if self.remaining_fraction is None:
            return None
        return clamp_fraction(1.0 - self.remaining_fraction)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "quota_id": self.quota_id,
            "group": self.group,
            "window": self.window,
            "used_fraction": self.used_fraction,
            "remaining_fraction": self.remaining_fraction,
            "reset_time": self.reset_time,
            "source": self.source,
            "metadata": self.metadata,
        }


@dataclass
class ProviderStatus:
    provider: str
    available: bool
    quotas: list[Quota] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": self.available,
            "quotas": [quota.to_dict() for quota in self.quotas],
            "details": self.details,
            "error": self.error,
        }


@dataclass
class WarmTarget:
    provider: str
    group: str
    model: str
    effort: str
    quota_ids: list[str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "group": self.group,
            "model": self.model,
            "effort": self.effort,
            "quota_ids": self.quota_ids,
            "reason": self.reason,
        }


BASE_CONFIG: dict[str, Any] = {
    "policy": {
        "trigger_min_used_fraction": 0.0,
        "state_reset_drop_fraction": 0.02,
    },
    "providers": {
        "codex": {
            "enabled": True,
            "binary": "",
            "model": "",
            "effort": "low",
            "assume_used": False,
            "timeout_seconds": 45,
            "warm_prompt": "Reply with exactly: OK",
        },
        "antigravity": {
            "enabled": True,
            "binary": "",
            "usage_timeout_seconds": 45,
            "warm_timeout_seconds": 180,
            "warm_prompt": "Reply with exactly: OK",
            "model_preferences": {
                "gemini": [
                    "gemini-3.5-flash-low",
                    "gemini-3.6-flash-low",
                    "gemini-3.7-flash-low",
                    "gemini-3.5-flash-medium",
                    "gemini-3.6-flash-medium",
                    "gemini-3.7-flash-medium",
                ],
                "third_party": [
                    "gpt-oss-120b-medium",
                    "claude-sonnet-4-6",
                    "claude-opus-4-6-thinking",
                ],
            },
        },
        "glm": {
            "enabled": True,
            "api_key_env": [
                "GLM_API_KEY",
                "ZAI_API_KEY",
                "ZAI_CODING_PLAN_API_KEY",
                "ZAI_API_TOKEN",
            ],
            "usage_url": "https://api.z.ai/api/monitor/usage",
            "legacy_usage_url": "https://api.z.ai/api/monitor/usage/quota/limit",
            "usage_auth_mode": "bearer",
            "usage_auth_fallback_to_raw": True,
            "request_base_url": "https://api.z.ai/api/coding/paas/v4",
            "request_auth_mode": "bearer",
            "model": "glm-5.3-flash",
            "reasoning_effort": "low",
            "max_tokens": 1,
            "timeout_seconds": 45,
            "warm_prompt": "Reply with exactly: OK",
        },
    },
}


class Provider:
    name = "provider"

    def __init__(self, config: Mapping[str, Any]):
        self.config = config

    def status(self) -> ProviderStatus:
        raise NotImplementedError

    def targets(self, status: ProviderStatus, due: list[Quota]) -> list[WarmTarget]:
        raise NotImplementedError

    def warm(self, target: WarmTarget) -> dict[str, Any]:
        raise NotImplementedError


def find_first_existing(candidates: Iterable[Path], fallback: str) -> str:
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return fallback


class AntigravityProvider(Provider):
    name = "antigravity"

    def __init__(self, config: Mapping[str, Any]):
        super().__init__(config)
        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        fallback = find_first_existing(
            [local_app_data / "agy" / "bin" / "agy.exe"],
            "agy",
        )
        self.binary = executable_parts(config.get("binary") or fallback, "agy")
        configured_profile = config.get("isolated_profile_dir")
        self.profile_dir = Path(str(configured_profile)).expanduser().resolve() if configured_profile else PROJECT_DIR / ".agy-quota-profile"
        self.last_models: list[str] = []

    def isolated_environment(self) -> dict[str, str]:
        mcp_config = self.profile_dir / ".gemini" / "config" / "mcp_config.json"
        save_json_file(mcp_config, {"mcpServers": {}})
        environment = os.environ.copy()
        environment["USERPROFILE"] = str(self.profile_dir)
        environment["HOME"] = str(self.profile_dir)
        return environment

    @staticmethod
    def group_name(name: str) -> str:
        lowered = name.lower()
        if "gemini" in lowered:
            return "gemini"
        if "claude" in lowered or "gpt" in lowered or "third" in lowered:
            return "third_party"
        return re.sub(r"[^a-z0-9]+", "_", lowered).strip("_") or "unknown"

    @staticmethod
    def window_name(value: str) -> str:
        lowered = value.lower()
        if "5" in lowered or "hour" in lowered:
            return "5h"
        if "week" in lowered:
            return "weekly"
        return lowered.replace(" ", "_")

    def status(self) -> ProviderStatus:
        timeout = float(self.config.get("usage_timeout_seconds", 45))
        command = [
            *self.binary,
            "-p",
            "/usage",
            "--output-format",
            "json",
            "--print-timeout",
            f"{int(timeout)}s",
        ]
        try:
            result = run_command(command, self.profile_dir, timeout + 5, self.isolated_environment())
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip().splitlines()
                raise RuntimeError(detail[-1] if detail else f"exit code {result.returncode}")
            payload = parse_json_blob(result.stdout)
            quotas = parse_antigravity_usage(payload)
            if not quotas:
                raise RuntimeError("/usage returned no quota buckets")
            return ProviderStatus(
                self.name,
                True,
                quotas,
                {"command": "agy /usage", "raw_status": payload.get("status") if isinstance(payload, Mapping) else None},
            )
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return ProviderStatus(self.name, False, error=str(exc))

    def discover_models(self) -> list[str]:
        timeout = float(self.config.get("usage_timeout_seconds", 45))
        try:
            result = run_command([*self.binary, "models"], self.profile_dir, timeout, self.isolated_environment())
        except RuntimeError:
            return []
        if result.returncode != 0:
            return []
        models: list[str] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("fetching "):
                continue
            model = line.split()[0]
            if model and re.match(r"^[a-z0-9][a-z0-9._-]+$", model, re.I):
                models.append(model)
        self.last_models = models
        return models

    def choose_model(self, group: str, models: Sequence[str]) -> tuple[str | None, str | None]:
        preferences = self.config.get("model_preferences", {})
        preferred = list(preferences.get(group, [])) if isinstance(preferences, Mapping) else []
        available = set(models)
        for model in preferred:
            if model in available:
                return model, effort_for_model(model)

        candidates = [model for model in models if model.startswith("gemini-")] if group == "gemini" else [
            model for model in models if model.startswith(("claude-", "gpt-"))
        ]
        if not candidates:
            return None, None
        candidates.sort(key=model_sort_key)
        selected = candidates[0]
        return selected, effort_for_model(selected)

    def targets(self, status: ProviderStatus, due: list[Quota]) -> list[WarmTarget]:
        if not due:
            return []
        models = self.discover_models()
        targets: list[WarmTarget] = []
        for group in sorted({quota.group for quota in due}):
            group_due = [quota for quota in due if quota.group == group]
            model, effort = self.choose_model(group, models)
            if not model or not effort:
                continue
            targets.append(WarmTarget(self.name, group, model, effort, [quota.quota_id for quota in group_due], "5h quota window is not started"))
        return targets

    def warm(self, target: WarmTarget) -> dict[str, Any]:
        timeout = float(self.config.get("warm_timeout_seconds", 180))
        command = [
            *self.binary,
            "-p",
            str(self.config.get("warm_prompt", "Reply with exactly: OK")),
            "--model",
            target.model,
            "--effort",
            target.effort,
            "--disable-slash-commands",
            "--output-format",
            "json",
            "--print-timeout",
            f"{int(timeout)}s",
        ]
        result = run_command(command, self.profile_dir, timeout + 5, self.isolated_environment())
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise RuntimeError(detail[-1] if detail else f"exit code {result.returncode}")
        try:
            payload = parse_json_blob(result.stdout)
        except (ValueError, json.JSONDecodeError):
            payload = {"response": result.stdout[-500:]}
        return {"success": True, "model": target.model, "effort": target.effort, "result": payload}


def parse_antigravity_usage(payload: Mapping[str, Any]) -> list[Quota]:
    command = payload.get("command", {}) if isinstance(payload, Mapping) else {}
    data = command.get("data", {}) if isinstance(command, Mapping) else {}
    groups = data.get("groups", []) if isinstance(data, Mapping) else []
    quotas: list[Quota] = []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            group_label = str(group.get("name", "Unknown"))
            group_id = AntigravityProvider.group_name(group_label)
            buckets = group.get("buckets", [])
            if not isinstance(buckets, list):
                continue
            for bucket in buckets:
                if not isinstance(bucket, Mapping):
                    continue
                remaining = as_fraction(bucket.get("remaining_fraction"))
                window = AntigravityProvider.window_name(str(bucket.get("window", bucket.get("name", "unknown"))))
                quota_id = f"antigravity:{group_id}:{bucket.get('id', window)}"
                quotas.append(
                    Quota(
                        "antigravity",
                        quota_id,
                        group_id,
                        window,
                        remaining,
                        str(bucket.get("reset_time")) if bucket.get("reset_time") else None,
                        "agy /usage",
                        {"group_name": group_label, "bucket_name": bucket.get("name"), "bucket_id": bucket.get("id")},
                    )
                )
    if quotas:
        return quotas

    response = payload.get("response", "")
    if not isinstance(response, str):
        return []
    for line in response.splitlines():
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) < 3 or not fields[1].lower().endswith("remaining"):
            continue
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", fields[2])
        if not match:
            continue
        remaining = clamp_fraction(float(match.group(1)) / 100.0)
        group_label = fields[0]
        group_id = AntigravityProvider.group_name(group_label)
        window = AntigravityProvider.window_name(fields[1])
        quota_id = f"antigravity:{group_id}:{window}"
        quotas.append(Quota("antigravity", quota_id, group_id, window, remaining, fields[3] if len(fields) > 3 else None, "agy /usage text"))
    return quotas


def model_sort_key(model: str) -> tuple[int, int, int, str]:
    lowered = model.lower()
    family_rank = 0 if "flash" in lowered or "oss" in lowered else 1 if "sonnet" in lowered else 2
    effort_rank = 0 if lowered.endswith("-low") else 1 if lowered.endswith("-medium") else 2 if lowered.endswith("-high") else 1
    version_match = re.search(r"(\d+(?:\.\d+)?)", lowered)
    version_rank = int(float(version_match.group(1)) * 10) if version_match else 999
    return family_rank, effort_rank, version_rank, lowered


def effort_for_model(model: str) -> str:
    lowered = model.lower()
    if lowered.endswith("-low"):
        return "low"
    if lowered.endswith("-high"):
        return "high"
    if lowered.endswith("-medium"):
        return "medium"
    return "low"


class CodexProvider(Provider):
    name = "codex"

    def __init__(self, config: Mapping[str, Any]):
        super().__init__(config)
        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        native_candidates = sorted(
            (local_app_data / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        fallback = find_first_existing(
            [*native_candidates, local_app_data / "pnpm" / "bin" / "codex.CMD", local_app_data / "pnpm" / "bin" / "codex.ps1"],
            "codex",
        )
        self.binary = executable_parts(config.get("binary") or fallback, "codex")
        self.last_models: list[dict[str, Any]] = []

    def app_server_batch(self) -> tuple[dict[int, Any], str]:
        timeout = float(self.config.get("timeout_seconds", 45))
        command = [*self.binary, "app-server", "--stdio"]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(PROJECT_DIR),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **hidden_subprocess_options(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Executable not found: {command[0]}") from exc
        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output_queue.put(line)
            output_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        def send(message: Mapping[str, Any]) -> None:
            if process.stdin is None:
                raise RuntimeError("Codex app-server stdin is unavailable")
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def next_message(deadline: float) -> Mapping[str, Any]:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"Codex app-server timed out after {timeout:g}s")
                try:
                    line = output_queue.get(timeout=remaining)
                except queue.Empty as exc:
                    raise RuntimeError(f"Codex app-server timed out after {timeout:g}s") from exc
                if line is None:
                    raise RuntimeError("Codex app-server closed before returning a response")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, Mapping):
                    return message

        deadline = time.monotonic() + timeout
        try:
            send({"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "quota-warmup", "title": "Quota Warmup", "version": VERSION}, "capabilities": {}}})
            initialize_response = next_message(deadline)
            if initialize_response.get("id") != 1:
                raise RuntimeError("Codex app-server returned an unexpected initialization response")
            if "error" in initialize_response:
                error = initialize_response["error"]
                message = error.get("message", str(error)) if isinstance(error, Mapping) else str(error)
                raise RuntimeError(f"Codex initialization failed: {message}")
            send({"method": "initialized", "params": {}})
            send({"method": "account/rateLimits/read", "id": 2})
            send({"method": "model/list", "id": 3, "params": {}})
        except (BrokenPipeError, OSError) as exc:
            process.kill()
            process.wait()
            raise RuntimeError(f"Codex app-server communication failed: {exc}") from exc
        responses: dict[int, Any] = {}
        while 2 not in responses:
            message = next_message(deadline)
            if isinstance(message.get("id"), int):
                responses[int(message["id"])] = message
        while 3 not in responses and time.monotonic() < deadline:
            try:
                message = next_message(deadline)
            except RuntimeError:
                break
            if isinstance(message.get("id"), int):
                responses[int(message["id"])] = message
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=max(1.0, min(5.0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        stderr = process.stderr.read() if process.stderr is not None else ""
        if 2 not in responses:
            detail = stderr.strip().splitlines()
            raise RuntimeError(detail[-1] if detail else "Codex app-server returned no rate-limit response")
        return responses, stderr

    def status(self) -> ProviderStatus:
        try:
            responses, _ = self.app_server_batch()
            response = responses[2]
            if "error" in response:
                error = response["error"]
                message = error.get("message", str(error)) if isinstance(error, Mapping) else str(error)
                raise RuntimeError(message)
            result = response.get("result", {})
            quotas = parse_codex_rate_limits(result)
            local_activity = self.local_activity(quotas)
            for quota in quotas:
                signal = local_activity.get(quota.window)
                if signal:
                    quota.metadata.update(signal)
            model_response = responses.get(3, {})
            self.last_models = parse_codex_models(model_response.get("result", {}) if isinstance(model_response, Mapping) else {})
            if not quotas:
                raise RuntimeError("Codex returned no rate-limit buckets")
            return ProviderStatus(self.name, True, quotas, {"source": "codex app-server account/rateLimits/read", "models": self.last_models, "activity_signal": "local Codex thread timestamps supplement rounded account percentages"})
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return ProviderStatus(self.name, False, error=str(exc))

    def local_activity(self, quotas: Sequence[Quota]) -> dict[str, dict[str, Any]]:
        configured = str(self.config.get("local_state_db", "")).strip()
        codex_home = Path(os.environ.get("CODEX_HOME", "")) if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
        database = Path(configured) if configured else codex_home / "state_5.sqlite"
        if not database.exists():
            return {}
        result: dict[str, dict[str, Any]] = {}
        try:
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2)
            try:
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(threads)")}
                timestamp_expression = "COALESCE(updated_at_ms, updated_at * 1000)" if "updated_at_ms" in columns else "updated_at * 1000"
                now_ms = int(time.time() * 1000)
                for quota in quotas:
                    cutoff = activity_cutoff_ms(quota, now_ms)
                    if cutoff is None:
                        continue
                    row = connection.execute(
                        f"SELECT COUNT(*), COALESCE(SUM(tokens_used), 0), MAX({timestamp_expression}) FROM threads WHERE tokens_used > 0 AND {timestamp_expression} >= ?",
                        (cutoff,),
                    ).fetchone()
                    thread_count = int(row[0] or 0) if row else 0
                    result[quota.window] = {
                        "activity_detected": thread_count > 0,
                        "activity_source": "local_codex_threads",
                        "activity_window_start_ms": cutoff,
                        "recent_thread_count": thread_count,
                        "recent_thread_tokens": int(row[1] or 0) if row else 0,
                        "latest_thread_activity_ms": int(row[2]) if row and row[2] is not None else None,
                    }
            finally:
                connection.close()
        except (OSError, sqlite3.Error, ValueError):
            return {}
        return result

    def choose_model(self) -> tuple[str, str]:
        configured = str(self.config.get("model", "")).strip()
        if configured:
            return configured, str(self.config.get("effort", "low"))
        candidates: list[tuple[str, list[str]]] = []
        for item in self.last_models:
            model_id = str(item.get("id", ""))
            efforts = [str(value).lower() for value in item.get("reasoning_efforts", [])]
            if model_id:
                candidates.append((model_id, efforts))
        if candidates:
            preferred_order = ["gpt-5.6-luna", "gpt-5.5", "gpt-5.4-mini", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.4"]
            for preferred in preferred_order:
                for model, efforts in candidates:
                    if model == preferred:
                        if "low" in efforts:
                            return model, "low"
                        if "minimal" in efforts:
                            return model, "minimal"
                        return model, str(self.config.get("effort", "low"))
            candidates.sort(key=lambda item: model_sort_key(item[0]))
            for model, efforts in candidates:
                if "low" in efforts:
                    return model, "low"
                if "minimal" in efforts:
                    return model, "minimal"
            return candidates[0][0], str(self.config.get("effort", "low"))
        return "gpt-5.6-luna", str(self.config.get("effort", "low"))

    def targets(self, status: ProviderStatus, due: list[Quota]) -> list[WarmTarget]:
        if not due:
            return []
        model, effort = self.choose_model()
        return [WarmTarget(self.name, "codex", model, effort, [quota.quota_id for quota in due], "5h quota window is not started")]

    def warm(self, target: WarmTarget) -> dict[str, Any]:
        timeout = float(self.config.get("warm_timeout_seconds", 180))
        command = [
            *self.binary,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "--model",
            target.model,
            "-c",
            f'model_reasoning_effort="{target.effort}"',
            "--json",
            str(self.config.get("warm_prompt", "Reply with exactly: OK")),
        ]
        result = run_command(command, PROJECT_DIR, timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise RuntimeError(detail[-1] if detail else f"exit code {result.returncode}")
        return {"success": True, "model": target.model, "effort": target.effort, "output_tail": result.stdout[-500:]}


def parse_codex_models(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("models", payload.get("data", [])) if isinstance(payload, Mapping) else []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        model_id = item.get("id") or item.get("model")
        if not model_id:
            continue
        efforts = item.get("supportedReasoningEfforts", item.get("reasoningEfforts", []))
        if isinstance(efforts, str):
            efforts = [efforts]
        normalized_efforts: list[str] = []
        if isinstance(efforts, list):
            for effort in efforts:
                if isinstance(effort, Mapping):
                    effort = effort.get("reasoningEffort", effort.get("id", ""))
                if effort:
                    normalized_efforts.append(str(effort))
        result.append({"id": str(model_id), "reasoning_efforts": normalized_efforts})
    return result


def quota_window(duration_minutes: Any, fallback: str) -> str:
    try:
        minutes = int(duration_minutes)
    except (TypeError, ValueError):
        return fallback
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "weekly"
    if minutes == 43200:
        return "monthly"
    return f"{minutes}m" if minutes else fallback


def timestamp_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        timestamp = float(value)
        if timestamp > 100_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return str(value)


def activity_cutoff_ms(quota: "Quota", now_ms: int | None = None) -> int | None:
    duration_minutes = int(quota.metadata.get("window_duration_mins", 0) or 0)
    if duration_minutes <= 0:
        return None
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff = now_ms - duration_minutes * 60 * 1000
    if quota.reset_time:
        try:
            reset = datetime.fromisoformat(quota.reset_time.replace("Z", "+00:00"))
            provider_window_start = int((reset - timedelta(minutes=duration_minutes)).timestamp() * 1000)
            cutoff = max(cutoff, provider_window_start)
        except ValueError:
            pass
    return cutoff


def parse_codex_rate_limits(payload: Mapping[str, Any]) -> list[Quota]:
    by_limit = payload.get("rateLimitsByLimitId") if isinstance(payload, Mapping) else None
    entries: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(by_limit, Mapping):
        entries.extend((str(key), value) for key, value in by_limit.items() if isinstance(value, Mapping))
    if not entries and isinstance(payload.get("rateLimits"), Mapping):
        rate_limits = payload["rateLimits"]
        entries.append((str(rate_limits.get("limitId", "codex")), rate_limits))

    quotas: list[Quota] = []
    for limit_id, limit in entries:
        for label in ("primary", "secondary"):
            bucket = limit.get(label)
            if not isinstance(bucket, Mapping):
                continue
            used = as_fraction(bucket.get("usedPercent"))
            remaining = None if used is None else clamp_fraction(1.0 - used)
            window = quota_window(bucket.get("windowDurationMins"), label)
            quota_id = f"codex:{limit_id}:{label}"
            quotas.append(Quota("codex", quota_id, "codex", window, remaining, timestamp_iso(bucket.get("resetsAt")), "codex app-server", {"limit_id": limit_id, "bucket": label, "plan_type": limit.get("planType"), "window_duration_mins": bucket.get("windowDurationMins")}))
    return quotas


class GLMProvider(Provider):
    name = "glm"

    def __init__(self, config: Mapping[str, Any]):
        super().__init__(config)

    def api_key(self) -> tuple[str | None, str | None]:
        configured = self.config.get("api_key_env", [])
        names = [configured] if isinstance(configured, str) else list(configured)
        for name in names:
            value = os.environ.get(str(name), "")
            if value:
                return value, str(name)
        return None, None

    @staticmethod
    def auth_headers(key: str, mode: str) -> dict[str, str]:
        authorization = key if mode == "raw" else f"Bearer {key}"
        return {"Authorization": authorization, "Accept-Language": "en-US,en", "Content-Type": "application/json"}

    def usage_payload(self) -> tuple[Any, str]:
        key, env_name = self.api_key()
        if not key:
            raise RuntimeError("no GLM API key found in configured environment variables")
        timeout = float(self.config.get("timeout_seconds", 45))
        mode = str(self.config.get("usage_auth_mode", "raw"))
        urls = [str(self.config.get("usage_url", "https://api.z.ai/api/monitor/usage"))]
        legacy_url = str(self.config.get("legacy_usage_url", "")).strip()
        if legacy_url and legacy_url not in urls:
            urls.append(legacy_url)
        modes = [mode]
        if bool(self.config.get("usage_auth_fallback_to_raw", True)) and mode != "raw":
            modes.append("raw")
        if bool(self.config.get("usage_auth_fallback_to_bearer", True)) and mode != "bearer":
            modes.append("bearer")
        last_failure = "unknown error"
        for url in urls:
            for attempt_mode in modes:
                status, payload = http_json(url, self.auth_headers(key, attempt_mode), timeout)
                business_code = payload.get("code") if isinstance(payload, Mapping) else None
                success = payload.get("success") if isinstance(payload, Mapping) else None
                accepted = status < 400 and success is not False and not (isinstance(business_code, int) and business_code >= 400)
                if accepted:
                    return payload, f"{url} ({attempt_mode}, {env_name})"
                last_failure = f"HTTP {status}, business code {business_code}"
        raise RuntimeError(f"GLM usage endpoints rejected the credential: {last_failure}")

    def status(self) -> ProviderStatus:
        try:
            payload, source = self.usage_payload()
            quotas = parse_glm_quotas(payload)
            if not quotas:
                raise RuntimeError("usage endpoint returned no recognized quota limits")
            activity = self.model_activity()
            if activity.get("available"):
                for quota in quotas:
                    if quota.window == "5h":
                        quota.metadata.update(
                            {
                                "activity_detected": bool(activity.get("activity_detected")),
                                "activity_source": "Z.AI model-usage",
                                "recent_model_calls": activity.get("model_calls"),
                                "recent_tokens": activity.get("tokens"),
                            }
                        )
            payload_root = payload.get("data", {}) if isinstance(payload, Mapping) else {}
            return ProviderStatus(self.name, True, quotas, {"source": source, "percentage_interpreted_as": "used", "plan_level": payload_root.get("level") if isinstance(payload_root, Mapping) else None, "activity": activity})
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return ProviderStatus(self.name, False, error=str(exc))

    def model_activity(self) -> dict[str, Any]:
        key, _ = self.api_key()
        if not key:
            return {"available": False, "error": "no GLM API key"}
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=5)
        query = urllib.parse.urlencode(
            {
                "startTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                "endTime": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        url = f"{str(self.config.get('activity_url', 'https://api.z.ai/api/monitor/usage/model-usage')).rstrip('?')}?{query}"
        configured_mode = str(self.config.get("usage_auth_mode", "bearer"))
        modes = [configured_mode, *[mode for mode in ("raw", "bearer") if mode != configured_mode]]
        last_failure = "unknown error"
        for mode in modes:
            status, payload = http_json(url, self.auth_headers(key, mode), float(self.config.get("timeout_seconds", 45)))
            business_code = payload.get("code") if isinstance(payload, Mapping) else None
            if status >= 400 or (isinstance(business_code, int) and business_code >= 400):
                last_failure = f"HTTP {status}, business code {business_code}"
                continue
            root = payload.get("data", payload) if isinstance(payload, Mapping) else {}
            if not isinstance(root, Mapping):
                return {"available": False, "error": "unexpected model-usage response"}
            calls = sum(int(value or 0) for value in root.get("modelCallCount", []) if isinstance(value, (int, float)))
            tokens = sum(int(value or 0) for value in root.get("tokensUsage", []) if isinstance(value, (int, float)))
            return {"available": True, "activity_detected": calls > 0 or tokens > 0, "model_calls": calls, "tokens": tokens, "window": "5h"}
        return {"available": False, "error": last_failure}

    def targets(self, status: ProviderStatus, due: list[Quota]) -> list[WarmTarget]:
        if not due:
            return []
        model = str(self.config.get("model", "glm-5.3-flash"))
        effort = str(self.config.get("reasoning_effort", "low"))
        return [WarmTarget(self.name, "glm", model, effort, [quota.quota_id for quota in due], "5h quota window is not started")]

    def warm(self, target: WarmTarget) -> dict[str, Any]:
        key, _ = self.api_key()
        if not key:
            raise RuntimeError("no GLM API key found in configured environment variables")
        base_url = str(self.config.get("request_base_url", "https://api.z.ai/api/coding/paas/v4")).rstrip("/")
        url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        payload = {
            "model": target.model,
            "messages": [{"role": "user", "content": str(self.config.get("warm_prompt", "Reply with exactly: OK"))}],
            "thinking": {"type": "enabled"},
            "reasoning_effort": target.effort,
            "max_tokens": int(self.config.get("max_tokens", 1)),
            "stream": False,
        }
        status, response = http_json(url, self.auth_headers(key, str(self.config.get("request_auth_mode", "bearer"))), float(self.config.get("timeout_seconds", 45)), "POST", payload)
        if status >= 400:
            message = response.get("message", response.get("error", "")) if isinstance(response, Mapping) else ""
            raise RuntimeError(f"GLM request returned HTTP {status}{': ' + str(message) if message else ''}")
        return {"success": True, "model": target.model, "effort": target.effort, "usage": response.get("usage") if isinstance(response, Mapping) else None}


def parse_glm_quotas(payload: Any) -> list[Quota]:
    root = payload.get("data", payload) if isinstance(payload, Mapping) else payload
    limits = root.get("limits") if isinstance(root, Mapping) else None
    if not isinstance(limits, list):
        return []
    quotas: list[Quota] = []
    for index, item in enumerate(limits):
        if not isinstance(item, Mapping):
            continue
        raw_percentage = item.get("percentage", item.get("usedPercent"))
        try:
            used = clamp_fraction(float(raw_percentage) / 100.0) if raw_percentage is not None else None
        except (TypeError, ValueError):
            used = None
        if used is None:
            continue
        limit_type = str(item.get("type", f"limit_{index}"))
        lowered = limit_type.lower()
        # Z.AI's legacy monitor endpoint uses TIME_LIMIT for the monthly MCP
        # allowance. It is not a model quota and must never trigger warming.
        if limit_type.upper() == "TIME_LIMIT" or "mcp" in lowered:
            continue
        unit = item.get("unit")
        if unit == 3:
            number = item.get("number", 5)
            window = f"{number}h" if number else "5h"
        elif unit == 6:
            window = "weekly"
        elif "token" in lowered or "5 hour" in lowered or "5h" in lowered:
            window = "5h"
        elif "week" in lowered:
            window = "weekly"
        elif "month" in lowered:
            window = "monthly"
        else:
            window = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_") or f"limit_{index}"
        quotas.append(Quota("glm", f"glm:{window}", "glm", window, clamp_fraction(1.0 - used), timestamp_iso(item.get("nextResetTime")), "Z.AI monitor usage", {"type": limit_type, "unit": unit, "number": item.get("number"), "raw_percentage": raw_percentage, "current_value": item.get("currentValue"), "usage": item.get("usage"), "remaining": item.get("remaining")}))
    return quotas


def build_providers(config: Mapping[str, Any], selected: Sequence[str] | None = None) -> list[Provider]:
    provider_configs = config.get("providers", {})
    names = list(selected or ("codex", "antigravity", "glm"))
    result: list[Provider] = []
    classes = {"codex": CodexProvider, "antigravity": AntigravityProvider, "glm": GLMProvider}
    for name in names:
        provider_config = provider_configs.get(name, {}) if isinstance(provider_configs, Mapping) else {}
        if not isinstance(provider_config, Mapping) or not provider_config.get("enabled", True):
            continue
        result.append(classes[name](provider_config))
    return result


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(json.dumps(BASE_CONFIG))
    if path.exists():
        loaded = load_json_file(path, {})
        if not isinstance(loaded, Mapping):
            raise RuntimeError(f"Config must be a JSON object: {path}")
        deep_merge(config, loaded)
    return config


def hold_is_active(entry: Mapping[str, Any], now: datetime | None = None) -> bool:
    value = entry.get("hold_until")
    if not value:
        return False
    try:
        until = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return until > (now or utc_now())
    except ValueError:
        return False


def quota_hold_minutes(quota: Quota) -> int:
    configured = int(quota.metadata.get("window_duration_mins", 0) or 0)
    if configured > 0:
        return configured
    match = re.fullmatch(r"(\d+)h", quota.window.lower())
    return int(match.group(1)) * 60 if match else 0


def state_bucket(state: dict[str, Any], quota: Quota, reset_drop_fraction: float) -> dict[str, Any]:
    buckets = state.setdefault("buckets", {})
    entry = buckets.setdefault(quota.quota_id, {})
    old_reset = entry.get("reset_time")
    old_used = entry.get("last_used_fraction")
    hold_active = hold_is_active(entry)
    reset_detected = bool(old_reset and quota.reset_time and old_reset != quota.reset_time and not hold_active)
    if old_used is not None and quota.used_fraction is not None:
        try:
            reset_detected = reset_detected or (not hold_active and quota.used_fraction + reset_drop_fraction < float(old_used))
        except (TypeError, ValueError):
            pass
    if quota.used_fraction is not None and quota.used_fraction <= 0 and not bool(quota.metadata.get("activity_detected")) and not hold_active:
        reset_detected = True
    if reset_detected:
        entry["kicked"] = False
        entry["last_kicked_at"] = None
        entry["attempted"] = False
        entry["last_attempt_at"] = None
        entry["hold_until"] = None
    entry.update(
        {
            "provider": quota.provider,
            "group": quota.group,
            "window": quota.window,
            "reset_time": quota.reset_time,
            "last_used_fraction": quota.used_fraction,
        }
    )
    entry.setdefault("kicked", False)
    entry.setdefault("attempted", False)
    return entry


def quota_decision(state: dict[str, Any], quota: Quota, threshold: float, reset_drop_fraction: float) -> dict[str, Any]:
    decision = {
        "provider": quota.provider,
        "group": quota.group,
        "quota_id": quota.quota_id,
        "window": quota.window,
        "used_fraction": quota.used_fraction,
        "reset_time": quota.reset_time,
        "activity_detected": bool(quota.metadata.get("activity_detected")),
    }
    if quota.window != "5h":
        return {**decision, "action": "ignore", "reason": "not a 5h warmup target"}

    entry = state_bucket(state, quota, reset_drop_fraction)
    if quota.used_fraction is None and not decision["activity_detected"]:
        return {**decision, "action": "skip", "reason": "quota usage is unknown"}
    if quota.used_fraction is not None and quota.used_fraction > threshold:
        return {**decision, "action": "skip", "reason": "5h window already started (reported usage)"}
    if decision["activity_detected"]:
        return {**decision, "action": "skip", "reason": "5h window already started (exact activity)"}
    if hold_is_active(entry):
        return {**decision, "action": "skip", "reason": "previous attempt is still held", "hold_until": entry.get("hold_until")}
    return {**decision, "action": "warm", "reason": "5h quota window is not started"}


def due_quotas(state: dict[str, Any], quotas: Sequence[Quota], threshold: float, reset_drop_fraction: float) -> list[Quota]:
    return [quota for quota in quotas if quota_decision(state, quota, threshold, reset_drop_fraction)["action"] == "warm"]


def mark_group_attempted(state: dict[str, Any], quotas: Sequence[Quota], target: WarmTarget) -> None:
    now_value = utc_now()
    now = utc_iso(now_value)
    for quota in quotas:
        if target.quota_ids and quota.quota_id not in target.quota_ids:
            continue
        entry = state_bucket(state, quota, 0.0)
        hold_minutes = quota_hold_minutes(quota)
        hold_until = utc_iso(now_value + timedelta(minutes=hold_minutes)) if hold_minutes > 0 else None
        entry.update({"attempted": True, "last_attempt_at": now, "hold_until": hold_until, "last_attempted_model": target.model, "last_attempted_effort": target.effort})


def mark_group_kicked(state: dict[str, Any], quotas: Sequence[Quota], target: WarmTarget) -> None:
    now = utc_iso()
    for quota in quotas:
        if target.quota_ids and quota.quota_id not in target.quota_ids:
            continue
        entry = state_bucket(state, quota, 0.0)
        entry.update({"attempted": True, "kicked": True, "last_kicked_at": now, "last_kicked_model": target.model, "last_kicked_effort": target.effort})


def acquire_run_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def release_run_lock(handle) -> None:
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def format_percent(value: float | None) -> str:
    if value is None:
        return "unknown"
    if 0 < value < 0.001:
        return "<0.1%"
    return f"{value * 100:.1f}%"


def print_status(statuses: Sequence[ProviderStatus], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps({"version": VERSION, "statuses": [status.to_dict() for status in statuses]}, indent=2))
        return
    for status in statuses:
        print(f"[{status.provider}] {'available' if status.available else 'unavailable'}")
        if status.error:
            print(f"  error: {status.error}")
        for quota in status.quotas:
            reset = f" reset={quota.reset_time}" if quota.reset_time else ""
            print(f"  {quota.group}/{quota.window}: used={format_percent(quota.used_fraction)} remaining={format_percent(quota.remaining_fraction)}{reset}")


def compact_statuses(statuses: Sequence[ProviderStatus]) -> list[dict[str, Any]]:
    return [
        {
            "provider": status.provider,
            "available": status.available,
            "error": status.error,
            "quotas": [
                {
                    "quota_id": quota.quota_id,
                    "group": quota.group,
                    "window": quota.window,
                    "used_fraction": quota.used_fraction,
                    "reset_time": quota.reset_time,
                    "activity_detected": bool(quota.metadata.get("activity_detected")),
                }
                for quota in status.quotas
            ],
        }
        for status in statuses
    ]


def read_log_entries(path: Path, last: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries[-last:]


def print_log_entries(entries: Sequence[Mapping[str, Any]], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(list(entries), indent=2))
        return
    if not entries:
        print("No scheduled run logs found.")
        return
    for entry in entries:
        outcomes = entry.get("outcomes", [])
        event = entry.get("event")
        if event:
            print(f"{entry.get('at', 'unknown time')} — {event}: {entry.get('reason') or entry.get('error') or ''}".rstrip())
            continue
        sent = sum(1 for outcome in outcomes if outcome.get("success")) if isinstance(outcomes, list) else 0
        failed = sum(1 for outcome in outcomes if not outcome.get("success")) if isinstance(outcomes, list) else 0
        summary = "no warmup sent" if not sent and not failed else f"warmups sent={sent}, failed={failed}"
        print(f"{entry.get('at', 'unknown time')} — {summary}")
        decisions = entry.get("decisions")
        if isinstance(decisions, list):
            for decision in decisions:
                reset = f", reset={decision['reset_time']}" if decision.get("reset_time") else ""
                hold = f", hold_until={decision['hold_until']}" if decision.get("hold_until") else ""
                print(f"  {decision.get('provider')}/{decision.get('group')}/{decision.get('window')}: used={format_percent(decision.get('used_fraction'))}, {decision.get('action')} — {decision.get('reason')}{reset}{hold}")
        else:
            print("  legacy entry; detailed decisions were not recorded")


def run_once(
    config: Mapping[str, Any],
    provider_names: Sequence[str] | None,
    state_path: Path,
    log_path: Path,
    live: bool,
    as_json: bool,
    force_providers: Sequence[str],
) -> int:
    state = load_json_file(state_path, {"version": 1, "buckets": {}})
    if not isinstance(state, dict):
        state = {"version": 1, "buckets": {}}
    state.setdefault("version", 1)
    state.setdefault("buckets", {})
    threshold = float(config.get("policy", {}).get("trigger_min_used_fraction", 0.0))
    reset_drop = float(config.get("policy", {}).get("state_reset_drop_fraction", 0.02))
    statuses: list[ProviderStatus] = []
    providers = build_providers(config, provider_names)
    provider_by_name = {provider.name: provider for provider in providers}
    targets: list[WarmTarget] = []
    decisions: list[dict[str, Any]] = []

    for provider in providers:
        status = provider.status()
        statuses.append(status)
        if status.available:
            due = due_quotas(state, status.quotas, threshold, reset_drop)
            decisions.extend(quota_decision(state, quota, threshold, reset_drop) for quota in status.quotas)
            targets.extend(provider.targets(status, due))
        else:
            decisions.append({"provider": provider.name, "action": "skip", "reason": status.error or "provider unavailable"})
        if provider.name in force_providers:
            if provider.name == "codex":
                model, effort = provider.choose_model()  # type: ignore[attr-defined]
                targets.append(WarmTarget(provider.name, "codex", model, effort, [], "explicitly forced"))
            elif provider.name == "glm":
                glm_config = provider.config
                targets.append(WarmTarget(provider.name, "glm", str(glm_config.get("model", "glm-5.3-flash")), str(glm_config.get("reasoning_effort", "low")), [], "explicitly forced"))

    if not live:
        output = {"mode": "dry-run", "statuses": [status.to_dict() for status in statuses], "targets": [target.to_dict() for target in targets]}
        if as_json:
            print(json.dumps(output, indent=2))
        else:
            print_status(statuses)
            if targets:
                print("Would warm:")
                for target in targets:
                    print(f"  {target.provider}/{target.group}: {target.model} ({target.effort}) — {target.reason}")
            else:
                print("No warmup is due.")
        return 0

    outcomes: list[dict[str, Any]] = []
    for target in targets:
        provider = provider_by_name.get(target.provider)
        if provider is None:
            outcomes.append({"target": target.to_dict(), "success": False, "error": "provider unavailable"})
            continue
        matching_quotas = [quota for status in statuses for quota in status.quotas if quota.provider == target.provider and (quota.quota_id in target.quota_ids if target.quota_ids else quota.group == target.group)]
        mark_group_attempted(state, matching_quotas, target)
        state["last_run_at"] = utc_iso()
        save_json_file(state_path, state)
        try:
            result = provider.warm(target)
            mark_group_kicked(state, matching_quotas, target)
            outcomes.append({"target": target.to_dict(), "success": True, "result": result})
        except RuntimeError as exc:
            outcomes.append({"target": target.to_dict(), "success": False, "error": str(exc)})

    state["last_run_at"] = utc_iso()
    save_json_file(state_path, state)
    append_jsonl(log_path, {"at": utc_iso(), "mode": "live", "decisions": decisions, "outcomes": outcomes, "statuses": compact_statuses(statuses)})
    output = {"mode": "live", "statuses": [status.to_dict() for status in statuses], "outcomes": outcomes}
    if as_json:
        print(json.dumps(output, indent=2))
    else:
        print_status(statuses)
        for outcome in outcomes:
            target = outcome["target"]
            state_label = "sent" if outcome["success"] else f"failed: {outcome['error']}"
            print(f"{state_label}: {target['provider']}/{target['group']} {target['model']} ({target['effort']})")
        if not outcomes:
            print("No warmup is due.")
    return 0 if all(outcome["success"] for outcome in outcomes) else 1


def background_python_executable(executable: str | Path | None = None) -> Path:
    python = Path(executable or sys.executable).resolve()
    pythonw = python.with_name("pythonw.exe")
    return pythonw if pythonw.exists() else python


def scheduler_command(name: str, every_minutes: int) -> list[str]:
    python = background_python_executable()
    script = Path(__file__).resolve()
    task_action = f'"{python}" "{script}" run --live'
    return ["schtasks.exe", "/Create", "/TN", name, "/SC", "MINUTE", "/MO", str(every_minutes), "/TR", task_action, "/F"]


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Warm provider quota windows with the smallest configured model.")
    root.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    root.add_argument("--state", type=Path, default=DEFAULT_STATE)
    root.add_argument("--log", type=Path, default=DEFAULT_LOG)
    subparsers = root.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Read provider quota status without sending model requests")
    status.add_argument("--provider", action="append", choices=["codex", "antigravity", "glm"])
    status.add_argument("--json", action="store_true", dest="as_json")

    run = subparsers.add_parser("run", help="Evaluate quotas; dry-run unless --live is supplied")
    run.add_argument("--provider", action="append", choices=["codex", "antigravity", "glm"])
    run.add_argument("--force-provider", action="append", choices=["codex", "antigravity", "glm"], default=[])
    run.add_argument("--live", action="store_true", help="Actually send warmup requests and update state")
    run.add_argument("--json", action="store_true", dest="as_json")

    install = subparsers.add_parser("install-task", help="Explicitly register a Windows Task Scheduler task")
    install.add_argument("--name", default="Quota Warmup")
    install.add_argument("--every-minutes", type=int, default=60)

    logs = subparsers.add_parser("logs", help="Show recent scheduled live-run decisions")
    logs.add_argument("--last", type=int, default=20)
    logs.add_argument("--json", action="store_true", dest="as_json")

    remove = subparsers.add_parser("remove-task", help="Explicitly remove a Windows Task Scheduler task")
    remove.add_argument("--name", default="Quota Warmup")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "install-task":
        if args.every_minutes < 1:
            print("--every-minutes must be at least 1", file=sys.stderr)
            return 2
        command = scheduler_command(args.name, args.every_minutes)
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if result.returncode:
            print((result.stderr or result.stdout).strip(), file=sys.stderr)
            return result.returncode
        print(f"Registered Task Scheduler task {args.name!r} every {args.every_minutes} minute(s).")
        return 0
    if args.command == "remove-task":
        result = subprocess.run(["schtasks.exe", "/Delete", "/TN", args.name, "/F"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if result.returncode:
            print((result.stderr or result.stdout).strip(), file=sys.stderr)
            return result.returncode
        print(f"Removed Task Scheduler task {args.name!r}.")
        return 0
    if args.command == "logs":
        if args.last < 1:
            print("--last must be at least 1", file=sys.stderr)
            return 2
        print_log_entries(read_log_entries(args.log, args.last), args.as_json)
        return 0

    config = load_config(args.config)
    provider_names = getattr(args, "provider", None)
    providers = build_providers(config, provider_names)
    if args.command == "status":
        statuses = [provider.status() for provider in providers]
        print_status(statuses, args.as_json)
        return 0
    if args.command == "run":
        if not args.live:
            return run_once(config, provider_names, args.state, args.log, False, args.as_json, args.force_provider)
        lock = acquire_run_lock(args.state.with_suffix(args.state.suffix + ".lock"))
        if lock is None:
            append_jsonl(args.log, {"at": utc_iso(), "mode": "live", "event": "skipped", "reason": "another run is already active"})
            print("Another quota-warmup run is already active; skipping this run.")
            return 0
        try:
            try:
                return run_once(config, provider_names, args.state, args.log, True, args.as_json, args.force_provider)
            except Exception as exc:
                append_jsonl(args.log, {"at": utc_iso(), "mode": "live", "event": "failed", "error": f"{type(exc).__name__}: {exc}"})
                print(f"Quota warmup failed: {exc}", file=sys.stderr)
                return 1
        finally:
            release_run_lock(lock)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
