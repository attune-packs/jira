"""Jira authentication, normalization, and action dispatch.

Adapted from StackStorm Exchange's Apache-2.0 ``jira`` pack version 3.3.0.
The implementation intentionally uses the supported python-jira API rather
than copying the source pack's global search monkey patch.
"""

from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse


class JiraPackError(RuntimeError):
    """Safe operator-facing configuration or operation error."""


def _fetch_key(ref: str) -> Dict[str, Any]:
    if not ref or not isinstance(ref, str):
        raise JiraPackError("credential_key must be a non-empty string")
    try:
        import attune
        from attune.api_client.api.secrets import get_key
    except ImportError as exc:
        raise JiraPackError("attune-sdk is required to resolve credential_key") from exc

    try:
        response = get_key.sync_detailed(ref, client=attune.context.client, decrypt=True)
    except Exception as exc:
        raise JiraPackError(f"unable to read credential Key {ref!r}") from exc
    status = int(response.status_code)
    if status == 404:
        raise JiraPackError(f"credential Key {ref!r} was not found")
    if status >= 400 or not response.parsed:
        raise JiraPackError(f"credential Key lookup failed with status {status}")
    value = response.parsed.data.value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise JiraPackError("credential Key must contain a JSON object") from exc
    if not isinstance(value, dict):
        raise JiraPackError("credential Key must contain an object")
    return value


def create_client(config: Mapping[str, Any]) -> Any:
    """Create a python-jira client for one credential object."""
    try:
        from jira import JIRA
    except ImportError as exc:
        raise JiraPackError("the jira Python package is not installed") from exc

    url = config.get("url") or config.get("base_url")
    parsed_url = urlparse(url) if isinstance(url, str) else None
    if parsed_url is None or parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise JiraPackError("credential Key requires an http(s) url")
    auth_method = config.get("auth_method", "api_token")
    if not isinstance(auth_method, str):
        raise JiraPackError("auth_method must be a string")
    verify = config.get("verify", True)
    validate = config.get("validate", False)
    if not isinstance(verify, bool) or not isinstance(validate, bool):
        raise JiraPackError("verify and validate must be booleans")
    timeout_value = config.get("timeout_seconds", 30)
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise JiraPackError("timeout_seconds must be a number")
    timeout = float(timeout_value)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 300:
        raise JiraPackError("timeout_seconds must be between 1 and 300")
    max_retries = config.get("max_retries", 3)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0 or max_retries > 10:
        raise JiraPackError("max_retries must be an integer between 0 and 10")

    kwargs: Dict[str, Any] = {
        "options": {"server": url.rstrip("/"), "verify": verify},
        "validate": validate,
        "timeout": timeout,
        "max_retries": max_retries,
    }
    username = config.get("username")
    token = config.get("token")
    password = config.get("password")

    for name, value in (("username", username), ("token", token), ("password", password)):
        if value is not None and not isinstance(value, str):
            raise JiraPackError(f"{name} must be a string")

    if auth_method == "oauth":
        private_key = config.get("rsa_private_key")
        cert_file = config.get("rsa_cert_file")
        if private_key is not None and not isinstance(private_key, str):
            raise JiraPackError("rsa_private_key must be a string")
        if cert_file is not None and not isinstance(cert_file, str):
            raise JiraPackError("rsa_cert_file must be a string")
        if not private_key and cert_file:
            with open(str(cert_file), encoding="utf-8") as handle:
                private_key = handle.read()
        required = {
            "access_token": config.get("oauth_token"),
            "access_token_secret": config.get("oauth_secret"),
            "consumer_key": config.get("consumer_key"),
            "key_cert": private_key,
        }
        if not all(required.values()):
            raise JiraPackError("oauth requires oauth_token, oauth_secret, consumer_key, and an RSA private key")
        kwargs["oauth"] = required
    elif auth_method in {"basic", "cookie"}:
        if not username or password is None:
            raise JiraPackError(f"{auth_method} authentication requires username and password")
        kwargs["auth" if auth_method == "cookie" else "basic_auth"] = (username, password)
    elif auth_method == "api_token":
        if not username or not token:
            raise JiraPackError("api_token authentication requires username and token")
        kwargs["basic_auth"] = (username, token)
    elif auth_method == "pat":
        if not token:
            raise JiraPackError("pat authentication requires token")
        kwargs["token_auth"] = token
    else:
        raise JiraPackError(f"unsupported auth_method {auth_method!r}")
    return JIRA(**kwargs)


def client_from_params(params: Mapping[str, Any]) -> tuple[Any, Dict[str, Any]]:
    key_ref = params.get("credential_key", "jira.credentials")
    config = _fetch_key(str(key_ref))
    return create_client(config), config


def to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_plain(item) for item in value]
    raw = getattr(value, "raw", None)
    if raw is not None:
        return to_plain(raw)
    return str(value)


def _resource_result(value: Any, operation: str) -> Any:
    if value is None:
        raise JiraPackError(f"{operation} returned no resource; the Jira deployment may not support this API")
    return to_plain(value)


def _name(value: Any) -> Optional[str]:
    return getattr(value, "name", None) if value is not None else None


def _display_name(value: Any) -> Optional[str]:
    return getattr(value, "displayName", None) if value is not None else None


def _attachment(value: Any) -> Dict[str, Any]:
    return {
        "filename": getattr(value, "filename", None),
        "size": getattr(value, "size", None),
        "created_at": getattr(value, "created", None),
        "content": getattr(value, "content", None),
    }


def _attachment_path(path_value: Any) -> Path:
    artifacts_dir = os.environ.get("ATTUNE_ARTIFACTS_DIR")
    if not artifacts_dir:
        raise JiraPackError("ATTUNE_ARTIFACTS_DIR is required for attachment uploads")
    root = Path(artifacts_dir).resolve()
    candidate = Path(str(path_value)).resolve()
    if candidate == root or root not in candidate.parents or not candidate.is_file():
        raise JiraPackError("attachment path must be a file under ATTUNE_ARTIFACTS_DIR")
    return candidate


def _comment(value: Any) -> Dict[str, Any]:
    return {"id": str(getattr(value, "id", "")), "body": to_plain(getattr(value, "body", None))}


def _issue_link(value: Any) -> Dict[str, Any]:
    outward = getattr(value, "outwardIssue", None)
    linked = outward or getattr(value, "inwardIssue", None)
    link_type = getattr(getattr(value, "type", None), "outward" if outward else "inward", None)
    fields = getattr(linked, "fields", None)
    return {
        "id": str(getattr(value, "id", "")),
        "key": getattr(linked, "key", None),
        "summary": getattr(fields, "summary", None),
        "status": _name(getattr(fields, "status", None)),
        "type": link_type,
    }


def issue_to_dict(
    issue: Any,
    *,
    include_comments: bool = False,
    include_attachments: bool = False,
    include_customfields: bool = False,
    include_components: bool = False,
    include_subtasks: bool = False,
    include_links: bool = False,
) -> Dict[str, Any]:
    fields = issue.fields
    permalink = issue.permalink()
    result: Dict[str, Any] = {
        "id": str(getattr(issue, "id", "")),
        "key": getattr(issue, "key", None),
        "url": permalink.split(" - ", 1)[0] if isinstance(permalink, str) else str(permalink),
        "summary": getattr(fields, "summary", None),
        "description": to_plain(getattr(fields, "description", None)),
        "status": _name(getattr(fields, "status", None)),
        "priority": _name(getattr(fields, "priority", None)),
        "resolution": _name(getattr(fields, "resolution", None)),
        "labels": list(getattr(fields, "labels", None) or []),
        "reporter": _display_name(getattr(fields, "reporter", None)),
        "assignee": _display_name(getattr(fields, "assignee", None)),
        "created_at": getattr(fields, "created", None),
        "updated_at": getattr(fields, "updated", None),
        "resolved_at": getattr(fields, "resolutiondate", None),
    }
    if include_comments:
        result["comments"] = [_comment(item) for item in getattr(getattr(fields, "comment", None), "comments", [])]
    if include_attachments:
        result["attachments"] = [_attachment(item) for item in getattr(fields, "attachment", [])]
    if include_customfields:
        raw_fields = (getattr(issue, "raw", {}) or {}).get("fields", {})
        result.update({key: to_plain(value) for key, value in raw_fields.items() if key.startswith("customfield_")})
    if include_components:
        result["components"] = [
            {"id": str(getattr(item, "id", "")), "name": getattr(item, "name", None)}
            for item in getattr(fields, "components", [])
        ]
    if include_subtasks:
        result["subtasks"] = [
            {
                "id": str(getattr(item, "id", "")),
                "key": getattr(item, "key", None),
                "summary": getattr(getattr(item, "fields", None), "summary", None),
            }
            for item in getattr(fields, "subtasks", [])
        ]
    if include_links:
        result["links"] = [_issue_link(item) for item in getattr(fields, "issuelinks", [])]
    return result


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("{{", "").replace("}}", "")
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    return value


def _without_none(values: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _status_response(response: Any) -> Dict[str, Any]:
    status = int(getattr(response, "status_code", 0))
    return {"success": status == 204, "status_code": status, "response_text": getattr(response, "text", "")}


def _find_gadget(client: Any, dashboard_id: str, gadget_id: str) -> Any:
    dashboard = client.dashboard(dashboard_id)
    try:
        return next(item for item in dashboard.gadgets if str(item.id) == str(gadget_id))
    except StopIteration as exc:
        raise JiraPackError(f"gadget {gadget_id!r} was not found on dashboard {dashboard_id!r}") from exc


def _issue_options(params: Mapping[str, Any]) -> Dict[str, bool]:
    return {
        "include_comments": bool(params.get("include_comments", False)),
        "include_attachments": bool(params.get("include_attachments", False)),
        "include_customfields": bool(params.get("include_customfields", False)),
        "include_components": bool(params.get("include_components", False)),
        "include_subtasks": bool(params.get("include_subtasks", False)),
        "include_links": bool(params.get("include_links", False)),
    }


def _search_limit(params: Mapping[str, Any]) -> int:
    value = params.get("max_results", 50)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 1000:
        raise JiraPackError("max_results must be an integer between 1 and 1000")
    return value


def _bulk_link(client: Any, params: Mapping[str, Any]) -> Dict[str, Any]:
    direction = params.get("direction", "outward")
    target = params["target_issue"]

    def link(issue_key: str) -> Dict[str, Any]:
        inward, outward = (target, issue_key) if direction == "outward" else (issue_key, target)
        response = client.create_issue_link(params.get("link_type", "relates to"), inward, outward)
        return {"issue_key": issue_key, "inward_issue": inward, "outward_issue": outward, "response": to_plain(response), "success": True}

    results: List[Dict[str, Any]] = []
    issue_keys = list(params["issue_key_list"])
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(issue_keys)))) as pool:
        futures = {pool.submit(link, key): key for key in issue_keys}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"issue_key": futures[future], "success": False, "error": type(exc).__name__})
    return {"success": all(item["success"] for item in results), "results": results}


def execute_action(operation: str, incoming: Mapping[str, Any]) -> Any:
    params = dict(incoming)
    client, config = client_from_params(params)
    params.pop("credential_key", None)

    if operation == "add_field_value":
        issue = client.issue(params["issue_key"])
        issue.add_field_value(params["field"], params["value"])
        return issue_to_dict(issue)
    if operation == "add_gadget":
        return _resource_result(client.add_gadget_to_dashboard(**_without_none(params)), operation)
    if operation == "assign_issue":
        return {"success": True, "response": to_plain(client.assign_issue(issue=params["issue"], assignee=params["assignee"]))}
    if operation == "attach_file_to_issue":
        with _attachment_path(params["file_path"]).open("rb") as handle:
            item = client.add_attachment(issue=params["issue_key"], attachment=handle, filename=params.get("file_name"))
        return {"issue": params["issue_key"], **_attachment(item)}
    if operation == "attach_files_to_issue":
        output = []
        for path in params["file_paths"]:
            with _attachment_path(path).open("rb") as handle:
                item = client.add_attachment(issue=params["issue_key"], attachment=handle, filename=None)
            output.append({"issue": params["issue_key"], **_attachment(item)})
        return output
    if operation == "bulk_link_issue":
        return _bulk_link(client, params)
    if operation == "comment_issue":
        return _comment(client.add_comment(params["issue_key"], params["comment_text"]))
    if operation == "copy_dashboard":
        return _resource_result(client.copy_dashboard(**_without_none(params)), operation)
    if operation == "create_dashboard":
        return _resource_result(client.create_dashboard(**_without_none(params)), operation)
    if operation == "create_issue":
        project = params.get("project") or config.get("project") or config.get("default_project")
        if not project:
            raise JiraPackError("create_issue requires project or credential Key default_project")
        fields = {"project": {"key": project}, "summary": params["summary"], "issuetype": {"name": params.get("type", "Task")}}
        if params.get("description"):
            fields["description"] = params["description"]
        fields.update(params.get("extra_fields") or {})
        return issue_to_dict(client.create_issue(fields=fields))
    if operation == "delete_dashboard_item_property":
        resource = client.dashboard_item_property(params["dashboard_id"], params["item_id"], params["property_key"])
        return _status_response(resource.delete(params["dashboard_id"], params["item_id"]))
    if operation == "delete_dashboard":
        return _status_response(client.dashboard(params["dashboard_id"]).delete())
    if operation == "get_available_gadgets":
        return [to_plain(item) for item in client.all_dashboard_gadgets()]
    if operation == "get_dashboard_gadgets":
        return [to_plain(item) for item in client.dashboard_gadgets(params["dashboard_id"])]
    if operation == "get_dashboard_item_property":
        return _resource_result(client.dashboard_item_property(params["dashboard_id"], params["item_id"], params["property_key"]), operation)
    if operation == "get_dashboard_item_property_keys":
        return [to_plain(item) for item in client.dashboard_item_property_keys(params["dashboard_id"], params["item_id"])]
    if operation == "get_issue":
        result = issue_to_dict(client.issue(params["issue_key"]), **_issue_options(params))
        return _sanitize(result) if params.get("sanitize_formatting") else result
    if operation == "get_issue_attachments":
        return [_attachment(item) for item in getattr(client.issue(params["issue_key"]).fields, "attachment", [])]
    if operation == "get_issue_comments":
        comments = getattr(getattr(client.issue(params["issue_key"]).fields, "comment", None), "comments", [])
        return [_comment(item) for item in comments]
    if operation == "get_issue_links":
        return [_issue_link(item) for item in getattr(client.issue(params["issue_key"]).fields, "issuelinks", [])]
    if operation == "link_issue":
        response = client.create_issue_link(params.get("link_type", "relates to"), params["inward_issue_key"], params["outward_issue_key"])
        return {"success": True, "response": to_plain(response)}
    if operation == "remove_gadget":
        return _status_response(_find_gadget(client, params["dashboard_id"], params["gadget_id"]).delete(params["dashboard_id"]))
    if operation == "search_issues":
        issues = client.search_issues(params["query"], startAt=params.get("start_at", 0), maxResults=_search_limit(params))
        return [issue_to_dict(item, **_issue_options(params)) for item in issues]
    if operation == "search_users":
        users = client.search_users(query=params["query"], startAt=params.get("start_at", 0), maxResults=_search_limit(params), includeActive=params.get("include_active", True), includeInactive=params.get("include_inactive", False))
        return [to_plain(item) for item in users]
    if operation == "set_dashboard_item_property":
        return _resource_result(client.set_dashboard_item_property(params["dashboard_id"], params["item_id"], params["property_key"], params["value"]), operation)
    if operation == "transition_issue":
        response = client.transition_issue(params["issue_key"], params["transition"], fields=params.get("fields"))
        return {"success": True, "response": to_plain(response)}
    if operation == "transition_issue_by_name":
        transition = next((item for item in client.transitions(params["issue"]) if item.get("name") == params["transition_name"]), None)
        if transition is None:
            raise JiraPackError(f"transition {params['transition_name']!r} was not found")
        return {"success": True, "response": to_plain(client.transition_issue(issue=params["issue"], transition=transition["id"]))}
    if operation == "update_dashboard":
        dashboard = client.dashboard(params["dashboard_id"])
        dashboard.update(**_without_none({"name": params["name"], "description": params.get("description"), "editPermissions": params.get("edit_permissions"), "sharePermissions": params.get("share_permissions")}))
        return to_plain(dashboard)
    if operation == "update_dashboard_automatic_refresh":
        return _status_response(client.update_dashboard_automatic_refresh_minutes(params["id"], params["minutes"]))
    if operation == "update_dashboard_item_property":
        resource = client.dashboard_item_property(params["dashboard_id"], params["item_id"], params["property_key"])
        return _resource_result(resource.update(params["dashboard_id"], params["item_id"], params["value"]), operation)
    if operation == "update_field_value":
        issue = client.issue(params["issue_key"])
        value: Any = params["value"]
        if params["field"] == "priority":
            value = {"name": value}
        elif params["field"] == "labels":
            value = value.split()
        issue.update(fields={params["field"]: value}, notify=params.get("notify", True))
        return issue_to_dict(issue)
    if operation == "update_gadget":
        gadget = _find_gadget(client, params["dashboard_id"], params["gadget_id"])
        updated = gadget.update(**_without_none({"dashboard_id": params["dashboard_id"], "color": params.get("color", "blue"), "position": params.get("position"), "title": params.get("title")}))
        return _resource_result(updated, operation)
    raise JiraPackError(f"unsupported Jira operation {operation!r}")
