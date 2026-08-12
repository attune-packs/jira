from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import yaml

PACK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK_ROOT))

from lib import jira_client


EXPECTED_ACTIONS = {
    "add_field_value", "add_gadget", "assign_issue", "attach_file_to_issue",
    "attach_files_to_issue", "bulk_link_issue", "comment_issue", "copy_dashboard",
    "create_dashboard", "create_issue", "delete_dashboard_item_property",
    "delete_dashboard", "get_available_gadgets", "get_dashboard_gadgets",
    "get_dashboard_item_property", "get_dashboard_item_property_keys", "get_issue",
    "get_issue_attachments", "get_issue_comments", "get_issue_links", "link_issue",
    "remove_gadget", "search_issues", "search_users", "set_dashboard_item_property",
    "transition_issue", "transition_issue_by_name", "update_dashboard",
    "update_dashboard_automatic_refresh", "update_dashboard_item_property",
    "update_field_value", "update_gadget",
}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    spec.loader.exec_module(module)
    return module


def make_issue(key="DEMO-1", issue_id="10001"):
    fields = SimpleNamespace(
        summary="Example", description=None, status=SimpleNamespace(name="Open"),
        priority=None, resolution=None, labels=[], reporter=None, assignee=None,
        created="2026-01-01T00:00:00Z", updated="2026-01-01T00:00:00Z",
        resolutiondate=None, attachment=[], comment=SimpleNamespace(comments=[]),
        components=[], subtasks=[], issuelinks=[],
    )
    return SimpleNamespace(
        id=issue_id, key=key,
        self=f"https://jira.example.invalid/rest/api/2/issue/{issue_id}",
        fields=fields,
        raw={"expand": "", "fields": {"created": fields.created, "fixVersions": [], "assignee": None, "issuetype": {"name": "Task"}}},
        permalink=lambda: f"https://jira.example.invalid/browse/{key}",
    )


class PackTests(unittest.TestCase):
    def test_action_metadata_covers_every_source_action(self):
        documents = [yaml.safe_load(path.read_text()) for path in sorted((PACK_ROOT / "actions").glob("*.yaml"))]
        self.assertEqual({doc["ref"].split(".", 1)[1] for doc in documents}, EXPECTED_ACTIONS)
        for doc in documents:
            self.assertEqual(doc["runner_type"], "python")
            self.assertEqual(doc["entry_point"], "jira_action.py")
            self.assertEqual(doc["parameter_delivery"], "stdin")
            self.assertEqual(doc["parameter_format"], "json")
            self.assertEqual(doc["output_format"], "json")
            self.assertEqual(doc["default_execution_permission_set_refs"], ["standard"])
            self.assertEqual(doc["parameters"]["credential_key"]["default"], "jira.credentials")
            self.assertEqual(set(doc["output"]), {"operation", "result"})

    def test_trigger_sensor_contracts_are_linked(self):
        sensor = yaml.safe_load((PACK_ROOT / "sensors" / "jira_issue_poll.yaml").read_text())
        triggers = {}
        for path in (PACK_ROOT / "triggers").glob("*.yaml"):
            document = yaml.safe_load(path.read_text())
            triggers[document["ref"]] = document
        self.assertEqual(set(sensor["trigger_types"]), set(triggers))
        for trigger in triggers.values():
            self.assertNotIn("credential_file", trigger["parameters"])
            credential = trigger["parameters"]["credential_key"]
            self.assertEqual(credential["default"], "jira.credentials")
            self.assertTrue(credential["key_ref"])
        self.assertEqual(triggers["jira.issues_tracker"]["output"]["fix_versions"]["type"], "array")
        self.assertIn("issue_browse_url", triggers["jira.issues_tracker"]["output"])

    def test_create_client_preserves_tls_and_auth_modes(self):
        calls = []

        class FakeJira:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        jira_module = ModuleType("jira")
        jira_module.JIRA = FakeJira
        base = {"url": "https://jira.example.invalid", "verify": False, "timeout_seconds": 15}
        with patch.dict(sys.modules, {"jira": jira_module}):
            jira_client.create_client({**base, "auth_method": "basic", "username": "u", "password": "p"})
            jira_client.create_client({**base, "auth_method": "cookie", "username": "u", "password": "p"})
            jira_client.create_client({**base, "auth_method": "api_token", "username": "u", "token": "t"})
            jira_client.create_client({**base, "auth_method": "pat", "token": "t"})
            jira_client.create_client({**base, "auth_method": "oauth", "oauth_token": "a", "oauth_secret": "s", "consumer_key": "c", "rsa_private_key": "PRIVATE"})

        self.assertEqual(len(calls), 5)
        self.assertTrue(all(call["options"]["verify"] is False for call in calls))
        self.assertTrue(all(call["max_retries"] == 3 for call in calls))
        self.assertEqual(calls[0]["basic_auth"], ("u", "p"))
        self.assertEqual(calls[1]["auth"], ("u", "p"))
        self.assertEqual(calls[2]["basic_auth"], ("u", "t"))
        self.assertEqual(calls[3]["token_auth"], "t")
        self.assertEqual(calls[4]["oauth"]["key_cert"], "PRIVATE")

    def test_create_client_rejects_invalid_credentials(self):
        jira_module = ModuleType("jira")
        jira_module.JIRA = lambda **kwargs: kwargs
        configs = [
            {},
            {"url": "https://jira.example.invalid", "auth_method": "basic"},
            {"url": "https://jira.example.invalid", "auth_method": "pat"},
            {"url": "https://jira.example.invalid", "auth_method": "unknown"},
            {"url": "https://jira.example.invalid", "auth_method": "pat", "token": "x", "timeout_seconds": 0},
            {"url": "https://jira.example.invalid", "auth_method": "pat", "token": "x", "verify": "false"},
        ]
        with patch.dict(sys.modules, {"jira": jira_module}):
            for config in configs:
                with self.subTest(config=config), self.assertRaises(jira_client.JiraPackError):
                    jira_client.create_client(config)

    def test_fetch_key_explicitly_requests_decryption(self):
        calls = {}
        get_key_module = ModuleType("attune.api_client.api.secrets.get_key")

        def sync_detailed(ref, *, client, decrypt):
            calls.update(ref=ref, client=client, decrypt=decrypt)
            data = SimpleNamespace(value={"url": "https://jira.example.invalid"})
            return SimpleNamespace(status_code=200, parsed=SimpleNamespace(data=data))

        get_key_module.sync_detailed = sync_detailed
        secrets_module = ModuleType("attune.api_client.api.secrets")
        secrets_module.get_key = get_key_module
        attune_module = ModuleType("attune")
        attune_module.context = SimpleNamespace(client="execution-client")
        modules = {
            "attune": attune_module,
            "attune.api_client": ModuleType("attune.api_client"),
            "attune.api_client.api": ModuleType("attune.api_client.api"),
            "attune.api_client.api.secrets": secrets_module,
        }
        with patch.dict(sys.modules, modules):
            value = jira_client._fetch_key("jira.credentials")
        self.assertEqual(value["url"], "https://jira.example.invalid")
        self.assertEqual(calls, {"ref": "jira.credentials", "client": "execution-client", "decrypt": True})

    def test_create_issue_merges_source_fields_last(self):
        captured = {}

        class Client:
            def create_issue(self, *, fields):
                captured.update(fields)
                return make_issue("OVERRIDE-1")

        with patch.object(jira_client, "client_from_params", return_value=(Client(), {"default_project": "DEMO"})):
            result = jira_client.execute_action("create_issue", {"credential_key": "jira.credentials", "summary": "Original", "type": "Task", "extra_fields": {"summary": "Override"}})
        self.assertEqual(captured["project"], {"key": "DEMO"})
        self.assertEqual(captured["summary"], "Override")
        self.assertEqual(result["key"], "OVERRIDE-1")

    def test_update_labels_preserves_source_whitespace_split(self):
        issue = make_issue()
        updated = {}
        issue.update = lambda **kwargs: updated.update(kwargs)
        client = SimpleNamespace(issue=lambda key: issue)
        with patch.object(jira_client, "client_from_params", return_value=(client, {})):
            jira_client.execute_action("update_field_value", {"credential_key": "jira.credentials", "issue_key": "DEMO-1", "field": "labels", "value": "one two", "notify": False})
        self.assertEqual(updated, {"fields": {"labels": ["one", "two"]}, "notify": False})

    def test_bulk_link_reports_partial_failures(self):
        class Client:
            def create_issue_link(self, link_type, inward, outward):
                if outward == "BAD-1":
                    raise RuntimeError("synthetic")
                return None

        with patch.object(jira_client, "client_from_params", return_value=(Client(), {})):
            result = jira_client.execute_action("bulk_link_issue", {"credential_key": "jira.credentials", "issue_key_list": ["OK-1", "BAD-1"], "target_issue": "TARGET-1", "direction": "outward", "link_type": "relates to"})
        self.assertFalse(result["success"])
        self.assertEqual({item["issue_key"] for item in result["results"]}, {"OK-1", "BAD-1"})
        self.assertEqual(sum(not item["success"] for item in result["results"]), 1)

    def test_attachment_paths_are_confined_to_artifact_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            root.mkdir()
            allowed = root / "upload.txt"
            allowed.write_text("synthetic")
            outside = Path(directory) / "outside.txt"
            outside.write_text("synthetic")
            with patch.dict(os.environ, {"ATTUNE_ARTIFACTS_DIR": str(root)}):
                self.assertEqual(jira_client._attachment_path(str(allowed)), allowed.resolve())
                with self.assertRaises(jira_client.JiraPackError):
                    jira_client._attachment_path(str(outside))

    def test_search_limit_is_bounded(self):
        self.assertEqual(jira_client._search_limit({"max_results": 1000}), 1000)
        for value in (0, 1001, True, "50"):
            with self.subTest(value=value), self.assertRaises(jira_client.JiraPackError):
                jira_client._search_limit({"max_results": value})

    def test_cloud_only_resource_none_is_an_action_error(self):
        client = SimpleNamespace(add_gadget_to_dashboard=lambda **kwargs: None)
        with patch.object(jira_client, "client_from_params", return_value=(client, {})):
            with self.assertRaises(jira_client.JiraPackError):
                jira_client.execute_action("add_gadget", {"credential_key": "jira.credentials", "dashboard_id": "1"})

    def test_sensor_baselines_existing_issues(self):
        sensor = load_module("jira_issue_poll_baseline", PACK_ROOT / "sensors" / "jira_issue_poll.py")
        calls = []

        class Client:
            def search_issues(self, query, **kwargs):
                calls.append(query)
                return [make_issue(issue_id="42")]

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            emitted = sensor.poll_once(Client(), {"project": "DEMO", "baseline_existing": True}, "jira.issues_tracker_for_apiv2", state, lambda payload: None)
            self.assertEqual(emitted, 0)
            self.assertEqual(json.loads(state.read_text())["last_issue_id"], "42")
        self.assertIn("ORDER BY id DESC", calls[0])

    def test_sensor_checkpoints_only_after_successful_emission(self):
        sensor = load_module("jira_issue_poll_checkpoint", PACK_ROOT / "sensors" / "jira_issue_poll.py")
        client = SimpleNamespace(search_issues=lambda *args, **kwargs: [make_issue("DEMO-1", "1"), make_issue("DEMO-2", "2")])

        def fail_second(payload):
            if payload["id"] == "2":
                raise RuntimeError("synthetic emission failure")
            return 101

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with self.assertRaises(RuntimeError):
                sensor.poll_once(client, {"project": "DEMO", "baseline_existing": False}, "jira.issues_tracker_for_apiv2", state, fail_second)
            self.assertEqual(json.loads(state.read_text())["last_issue_id"], "1")

    def test_sensor_does_not_checkpoint_none_event_id(self):
        sensor = load_module("jira_issue_poll_none", PACK_ROOT / "sensors" / "jira_issue_poll.py")
        client = SimpleNamespace(search_issues=lambda *args, **kwargs: [make_issue("DEMO-1", "1")])
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with self.assertRaises(RuntimeError):
                sensor.poll_once(client, {"project": "DEMO", "baseline_existing": False}, "jira.issues_tracker_for_apiv2", state, lambda payload: None)
            self.assertFalse(state.exists())

    def test_empty_project_baseline_is_initialized(self):
        sensor = load_module("jira_issue_poll_empty", PACK_ROOT / "sensors" / "jira_issue_poll.py")
        responses = [[], [make_issue("DEMO-1", "1")]]
        client = SimpleNamespace(search_issues=lambda *args, **kwargs: responses.pop(0))
        emitted = []
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.assertEqual(sensor.poll_once(client, {"project": "DEMO", "baseline_existing": True}, "jira.issues_tracker_for_apiv2", state, emitted.append), 0)
            self.assertTrue(json.loads(state.read_text())["initialized"])
            self.assertEqual(sensor.poll_once(client, {"project": "DEMO", "baseline_existing": True}, "jira.issues_tracker_for_apiv2", state, lambda payload: emitted.append(payload) or 102), 1)
        self.assertEqual([item["id"] for item in emitted], ["1"])

    def test_sensor_payloads_match_both_trigger_shapes(self):
        sensor = load_module("jira_issue_poll_payload", PACK_ROOT / "sensors" / "jira_issue_poll.py")
        issue = make_issue()
        legacy = sensor._payload(issue, "DEMO", "https://jira.example.invalid/", "jira.issues_tracker")
        raw = sensor._payload(issue, "DEMO", "https://jira.example.invalid/", "jira.issues_tracker_for_apiv2")
        self.assertEqual(set(legacy), {"project", "issue_name", "issue_url", "issue_browse_url", "created", "assignee", "fix_versions", "issue_type"})
        self.assertEqual(set(raw), {"project", "id", "expand", "issue_key", "issue_url", "issue_browse_url", "fields"})
        self.assertEqual(legacy["assignee"], {})
        self.assertEqual(raw["issue_browse_url"], "https://jira.example.invalid/browse/DEMO-1")

    def test_sensor_rule_config_cannot_override_credentials(self):
        sensor = load_module("jira_issue_poll_config", PACK_ROOT / "sensors" / "jira_issue_poll.py")
        credentials = {"url": "https://jira.example.invalid", "auth_method": "pat", "token": "REDACTED"}
        config = {"project": "DEMO", "url": "https://attacker.invalid", "auth_method": "basic"}
        merged = {**credentials, **{name: config[name] for name in sensor.POLL_CONFIG_FIELDS if name in config}}
        self.assertEqual(merged["url"], "https://jira.example.invalid")
        self.assertEqual(merged["auth_method"], "pat")
        self.assertEqual(merged["project"], "DEMO")

    def test_action_entrypoint_rejects_malformed_json_without_echoing_it(self):
        module = load_module("jira_action_test", PACK_ROOT / "actions" / "jira_action.py")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdin", SimpleNamespace(read=lambda: '{"token":"SECRET"')), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(module.main(), 1)
        self.assertNotIn("SECRET", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_pack_contains_no_secret_fixture_values(self):
        forbidden = [
            "BEGIN " + "PRIVATE KEY",
            "Authorization" + ": Bearer",
            "password" + ": secret",
        ]
        for path in PACK_ROOT.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".yaml", ".md", ".txt"}:
                text = path.read_text(encoding="utf-8")
                self.assertFalse(any(value in text for value in forbidden), str(path))


if __name__ == "__main__":
    unittest.main()
