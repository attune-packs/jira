#!/usr/bin/env python3
"""Rule-targeted Jira issue polling sensor with per-rule checkpoints.

Adapted from StackStorm Exchange's Apache-2.0 Jira polling sensors.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

_PACK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACK_ROOT not in sys.path:
    sys.path.insert(0, _PACK_ROOT)

from lib.jira_client import _fetch_key, create_client, to_plain  # noqa: E402

POLL_CONFIG_FIELDS = {
    "project",
    "poll_interval_seconds",
    "page_size",
    "start_after_issue_id",
    "baseline_existing",
}


def _rule_id(rule: Any) -> int:
    return int(getattr(rule, "rule_id", 0) or 0)


def _state_path(rule_id: int, trigger_ref: str, project: str) -> Path:
    root = Path(os.environ.get("ATTUNE_ARTIFACTS_DIR", "/tmp")) / "jira-sensor-state"
    root.mkdir(parents=True, exist_ok=True)
    safe_trigger = trigger_ref.replace(".", "_")
    return root / f"rule_{rule_id}_{safe_trigger}_{project}.json"


def _load_state(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(path: Path, issue_id: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump({"initialized": True, "last_issue_id": None if issue_id is None else str(issue_id)}, handle)
        temporary = handle.name
    os.replace(temporary, path)


def _payload(issue: Any, project: str, base_url: str, trigger_ref: str) -> Dict[str, Any]:
    raw = getattr(issue, "raw", {}) or {}
    fields = raw.get("fields", {}) or {}
    browse_url = f"{base_url.rstrip('/')}/browse/{issue.key}"
    if trigger_ref == "jira.issues_tracker":
        issue_type = fields.get("issuetype") or {}
        return {
            "project": project,
            "issue_name": str(issue.key),
            "issue_url": str(getattr(issue, "self", raw.get("self", ""))),
            "issue_browse_url": browse_url,
            "created": str(fields.get("created", "")),
            "assignee": to_plain(fields.get("assignee")) or {},
            "fix_versions": to_plain(fields.get("fixVersions") or []),
            "issue_type": str(issue_type.get("name", "")),
        }
    return {
        "project": project,
        "id": str(issue.id),
        "expand": str(raw.get("expand", "")),
        "issue_key": str(issue.key),
        "issue_url": str(getattr(issue, "self", raw.get("self", ""))),
        "issue_browse_url": browse_url,
        "fields": to_plain(fields),
    }


def poll_once(
    client: Any,
    config: Mapping[str, Any],
    trigger_ref: str,
    state_path: Path,
    emit: Callable[[Dict[str, Any]], Any],
) -> int:
    """Poll one page and checkpoint each successfully emitted issue."""
    project = str(config.get("project", ""))
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", project):
        raise ValueError("project must be a Jira project key")
    page_size = max(1, min(100, int(config.get("page_size", 50))))
    state = _load_state(state_path)
    initialized = bool(state.get("initialized", False))
    cursor = state.get("last_issue_id")
    if not initialized:
        cursor = config.get("start_after_issue_id")
    if not initialized and cursor is None and bool(config.get("baseline_existing", True)):
        newest = client.search_issues(f"project={project} ORDER BY id DESC", startAt=0, maxResults=1)
        _save_state(state_path, str(newest[0].id) if newest else None)
        return 0

    where = f"project={project}"
    if cursor is not None:
        where += f" AND id > {int(cursor)}"
    issues = client.search_issues(f"{where} ORDER BY id ASC", startAt=0, maxResults=page_size)
    emitted = 0
    base_url = str(config.get("url") or config.get("base_url") or "")
    for issue in issues:
        event_id = emit(_payload(issue, project, base_url, trigger_ref))
        if event_id is None:
            raise RuntimeError("Attune event emission failed")
        _save_state(state_path, str(issue.id))
        emitted += 1
    return emitted


def _production_sensor() -> type:
    import attune

    class JiraIssuePollSensor(attune.PollingSensor):
        def setup(self) -> None:
            self.interval = 5.0
            self._next_due: Dict[int, float] = {}
            self._failures: Dict[int, int] = {}
            self._poll_locks: Dict[int, threading.Lock] = {}

        def poll(self, rule: Any) -> None:
            rule_id = _rule_id(rule)
            config = dict(rule.trigger_params or {})
            now = time.monotonic()
            if now < self._next_due.get(rule_id, 0):
                return
            interval = max(5, min(3600, int(config.get("poll_interval_seconds", 30))))
            self._next_due[rule_id] = now + interval
            trigger_ref = str(rule.trigger_ref)
            lock = self._poll_locks.setdefault(rule_id, threading.Lock())
            if not lock.acquire(blocking=False):
                self.logger.warning("rule %s Jira poll is already running", rule_id)
                return
            try:
                credentials = _fetch_key(str(config.get("credential_key", "jira.credentials")))
                merged = {**credentials, **{name: config[name] for name in POLL_CONFIG_FIELDS if name in config}}
                client = create_client(merged)

                def emit_checked(payload: Dict[str, Any]) -> int:
                    event_id = self.emit(payload, rule=rule, target_rule=True)
                    if event_id is None:
                        raise RuntimeError("Attune event emission failed")
                    return event_id

                count = poll_once(
                    client,
                    merged,
                    trigger_ref,
                    _state_path(rule_id, trigger_ref, str(config.get("project", ""))),
                    emit_checked,
                )
                self._failures[rule_id] = 0
                if count:
                    self.logger.info("rule %s emitted %s Jira issue event(s)", rule_id, count)
            except Exception as exc:
                failures = self._failures.get(rule_id, 0) + 1
                self._failures[rule_id] = failures
                delay = min(300, interval * (2 ** min(failures - 1, 8)))
                self._next_due[rule_id] = time.monotonic() + delay
                self.logger.warning("rule %s Jira poll failed: %s", rule_id, type(exc).__name__)
            finally:
                lock.release()

    return JiraIssuePollSensor


def main() -> None:
    import attune

    attune.run_sensor(_production_sensor())


if __name__ == "__main__":
    main()
