# Jira Attune Pack

Attune actions and polling triggers for Jira Cloud and Jira Server/Data Center.
This pack is an Apache-2.0 adaptation of
[`StackStorm-Exchange/stackstorm-jira`](https://github.com/StackStorm-Exchange/stackstorm-jira)
version 3.3.0 (`a11138dc0cba9669e8db0660d0de45dee57b8aab`).

## Assumptions And Scope

- The target pack ref is `jira` and the local development directory is this repository.
- Python 3.10 or newer, Git for the pinned Attune SDK dependency, and outbound HTTPS access to GitHub/PyPI and Jira are available on workers during runtime setup/execution.
- Tests must be deterministic and make no external network calls.
- Actions and sensors use encrypted Attune Keys. Managed sensor tokens receive exact Key-ref access derived from each active rule's trigger parameters.
- The `jira>=3.8,<4` Python library remains the API compatibility layer. Jira Cloud and Server/Data Center can differ in REST and search behavior.
- There were no source workflows, rules, schedules, or work queues to translate.

## Setup

Create an encrypted, pack-owned Attune Key with ref `jira.credentials`. Actions
request the reserved `standard` execution scope to decrypt this Key.
The Key value is an object; use only the fields required by one authentication
method:

```json
{
  "url": "https://example.atlassian.net",
  "auth_method": "api_token",
  "username": "user@example.invalid",
  "token": "REDACTED_API_TOKEN",
  "verify": true,
  "timeout_seconds": 30,
  "max_retries": 3,
  "default_project": "DEMO"
}
```

Supported `auth_method` values:

| Method | Required Key fields | Notes |
|---|---|---|
| `api_token` | `username`, `token` | Jira Cloud email and API token through HTTP Basic authentication. |
| `pat` | `token` | Bearer personal access token, typically Server/Data Center. |
| `basic` | `username`, `password` | Password-based HTTP Basic authentication. |
| `cookie` | `username`, `password` | Preserves the source python-jira `auth` mode. |
| `oauth` | `oauth_token`, `oauth_secret`, `consumer_key`, `rsa_private_key` | OAuth 1.0a. `rsa_cert_file` is also accepted but requires worker-local placement. |

`verify` defaults to `true`, `validate` defaults to `false`, and
`timeout_seconds` defaults to 30 with an allowed range of 1 to 300.
`max_retries` is passed explicitly to python-jira and defaults to the source
library behavior of 3, with an allowed range of 0 to 10. Set it to 0 for
non-idempotent mutations when duplicate side effects are unacceptable. Unlike
the source pack, TLS verification settings apply to every authentication method.

Every action accepts `credential_key`, defaulting to `jira.credentials`. A
different pack-owned Key can be selected per execution.

## Action Usage

Inputs are one flat JSON object. Structured output has a stable envelope:

```json
{"operation":"get_issue","result":{"key":"DEMO-1"}}
```

Representative commands:

```bash
attune action execute jira.get_issue \
  --params-json '{"issue_key":"DEMO-1","include_comments":true}' \
  --watch

attune action execute jira.create_issue \
  --params-json '{"summary":"Synthetic example","type":"Task","project":"DEMO"}' \
  --watch
```

Attachment actions read only files under the selected worker's
`ATTUNE_ARTIFACTS_DIR`. Multiple attachments
are uploaded sequentially and are not rolled back if a later upload fails.
Mutating actions are not automatically retried because comments, links,
attachments, and issue creation are not generally idempotent.

`bulk_link_issue` retains a maximum concurrency of ten. Unlike the source,
worker exceptions are collected in `result.results` and reflected by
`result.success`; successful links are not compensated after partial failure.
The action exits zero after completing the fan-out even when this business
result is false, so workflows must inspect `result.success` when partial
failure should fail the parent flow.

Dashboard and gadget operations are primarily Jira Cloud APIs in python-jira.
Confirm support before enabling them against Server/Data Center deployments.

## Polling Events

The `jira.jira_issue_poll` sensor services two source-compatible trigger refs:

| Trigger | Payload |
|---|---|
| `jira.issues_tracker` | Normalized issue identity, timestamps, assignee (empty object when unassigned), fix versions, and issue type. |
| `jira.issues_tracker_for_apiv2` | Issue identity plus complete raw Jira `fields`. The historical name is retained; it does not force REST API v2. |

Set each rule's `credential_key` to the Attune Key containing its Jira
credentials; it defaults to `jira.credentials`. The trigger schema marks this
parameter as a Key ref, so Attune includes only the Keys selected by active
rules in the managed sensor's signed access token. The sensor resolves the Key
for each rule independently and does not require a worker credential file.

Rule trigger parameters include `credential_key`, required `project`,
`poll_interval_seconds` (5-3600), `page_size` (1-100), optional
`start_after_issue_id`, and `baseline_existing`.

By default, first startup records the newest existing issue and emits only
later issues, matching the source. Set `baseline_existing: false` to backfill
from the beginning, or provide `start_after_issue_id` for an explicit starting
watermark. Events target the active rule as `rule_<numeric-id>`.

The sensor writes an atomic checkpoint after each successful event under
`ATTUNE_ARTIFACTS_DIR/jira-sensor-state`. A failed event is retried on a later
poll and can duplicate the last successfully delivered event if persistence
fails. Transient failures use exponential backoff capped at 300 seconds.

The checkpoint is filesystem-backed and is not a distributed lease. Active
sensor replicas using unshared artifact storage can emit duplicates, and a
worker migration without shared state can repeat the initial-baseline behavior.
Use one eligible sensor worker or shared persistent artifact storage until a
durable Attune cache/checkpoint contract is selected.

## Source Inventory And Fidelity

All actions use stdin JSON, the shared `lib/jira_client.py` client, explicit
request timeouts, nonzero process exits for action failures, and JSON-safe
serialization. Source StackStorm `(success, value)` tuples are normalized
inside the common `result` envelope.

| Source | Attune target | Fidelity | Important differences | Follow-up |
|---|---|---|---|---|
| `pack.yaml`, `config.schema.yaml`, `jira.yaml.example` | `pack.yaml`, encrypted `jira.credentials` Key | adapted | Secrets moved out of config; project is no longer globally required. | Configure a pack-owned encrypted Key after installation. |
| `actions/lib/base.py` | `lib/jira_client.py` | adapted | Explicit Key lookup, timeout validation, and TLS verification for all auth modes. | Integration-test each enabled auth mode. |
| `actions/lib/formatters.py`, `utils.py` | `lib/jira_client.py` normalization | adapted | Null handling is safer; output is under `result`. | Compare rich-text/ADF fields against the target Jira deployment. |
| `actions/lib/patched_search.py` | Standard python-jira search API | partial | Unattributed monkey patch was not copied; Cloud token pagination and nonzero offsets may differ. | Verify Cloud enhanced-search behavior before publication. |
| `add_field_value` | `jira.add_field_value` | adapted | Same mutation and inputs; output uses the common envelope. | Test target custom-field type. |
| `add_gadget` | `jira.add_gadget` | adapted | Same client call; URI/module-key exclusivity remains Jira-validated. | Add deployment-specific gadget test. |
| `assign_issue` | `jira.assign_issue` | adapted | Jira failures now fail execution instead of returning a successful tuple. | Verify account ID versus username semantics. |
| `attach_file_to_issue` | `jira.attach_file_to_issue` | adapted | Worker-local reads are constrained to `ATTUNE_ARTIFACTS_DIR`; output envelope changed. | Apply worker placement and file-size policy. |
| `attach_files_to_issue` | `jira.attach_files_to_issue` | adapted | Reads are artifact-root constrained; sequential partial side effects remain; no rollback. | Use a workflow if compensation is required. |
| `bulk_link_issue` | `jira.bulk_link_issue` | adapted | Ten-way concurrency retained; per-item failures are now observable. | Decide whether partial failure should fail parent execution. |
| `comment_issue` | `jira.comment_issue` | adapted | Plain comment body retained; Cloud ADF behavior is Jira-dependent. | Validate target comment format. |
| `copy_dashboard` | `jira.copy_dashboard` | adapted | Same API operation; raw result is enveloped. | Verify permission object shapes. |
| `create_dashboard` | `jira.create_dashboard` | adapted | Same API operation; raw result is enveloped. | Verify permission object shapes. |
| `create_issue` | `jira.create_issue` | adapted | `extra_fields` still overrides base fields; default project comes from the Key. | Validate Cloud ADF description fields. |
| `delete_dashboard_item_property` | `jira.delete_dashboard_item_property` | adapted | Tuple becomes explicit status object. | None. |
| `delete_dashboard` (`delete_dashbord.yaml`) | `jira.delete_dashboard` | adapted | Correct target filename; tuple becomes status object. | None. |
| `get_available_gadgets` | `jira.get_available_gadgets` | adapted | Raw items are JSON-normalized and enveloped. | Verify Jira pagination behavior. |
| `get_dashboard_gadgets` | `jira.get_dashboard_gadgets` | adapted | Raw items are JSON-normalized and enveloped. | None. |
| `get_dashboard_item_property` | `jira.get_dashboard_item_property` | adapted | Raw property is JSON-normalized and enveloped. | None. |
| `get_dashboard_item_property_keys` | `jira.get_dashboard_item_property_keys` | adapted | Raw keys are JSON-normalized and enveloped. | None. |
| `get_issue` | `jira.get_issue` | adapted | Source inclusion flags and lossy brace sanitization retained; safer null handling. | Avoid sanitization unless legacy templates require it. |
| `get_issue_attachments` | `jira.get_issue_attachments` | adapted | Returns metadata and URLs, not file bytes, under the common envelope. | None. |
| `get_issue_comments` | `jira.get_issue_comments` | adapted | Source intentionally drops author/timestamp/visibility fields. | Extend contract only as a versioned change. |
| `get_issue_links` | `jira.get_issue_links` | adapted | Same normalized fields with safer null handling. | None. |
| `link_issue` | `jira.link_issue` | adapted | Same non-idempotent mutation; response is enveloped. | Do not retry automatically. |
| `remove_gadget` | `jira.remove_gadget` | adapted | Missing gadget now raises a clear error instead of `StopIteration`. | None. |
| `search_issues` | `jira.search_issues` | partial | No source monkey patch; Cloud offset/token semantics depend on python-jira/Jira version; unbounded `max_results: 0` is replaced by a 1000-result cap. | Run Cloud and Data Center pagination integration tests. |
| `search_users` | `jira.search_users` | adapted | Raw privacy-dependent user shape remains deployment-specific; results are capped at 1000. | Validate account ID fields on Jira Cloud. |
| `set_dashboard_item_property` | `jira.set_dashboard_item_property` | adapted | Same mutation and object input; output is enveloped. | None. |
| `transition_issue` | `jira.transition_issue` | adapted | Optional `fields` omission is fixed; failure exits nonzero. | Do not retry automatically. |
| `transition_issue_by_name` | `jira.transition_issue_by_name` | adapted | Missing exact name now fails before sending a null transition ID. | Consider transition-ID use for localization stability. |
| `update_dashboard` | `jira.update_dashboard` | adapted | Same camelCase permission mapping; normalized output. | Verify returned resource freshness. |
| `update_dashboard_automatic_refresh` | `jira.update_dashboard_automatic_refresh` | adapted | Tuple becomes status object; nonnegative minutes enforced. | Verify accepted Jira ranges. |
| `update_dashboard_item_property` | `jira.update_dashboard_item_property` | adapted | Same mutation; normalized output. | None. |
| `update_field_value` | `jira.update_field_value` | adapted | Source priority and whitespace label formatting retained. | Complex fields should use Jira-native objects in a future action. |
| `update_gadget` | `jira.update_gadget` | adapted | Source default color behavior retained; missing gadget error improved. | Omit action when an unchanged color cannot be tolerated. |
| `jira_sensor.py` / `issues_tracker` | `jira.jira_issue_poll` / `jira.issues_tracker` | partial | Correct schema, targeted events, checkpoint-after-delivery, backoff, and per-rule scoped Attune Key lookup; filesystem state is not a distributed lease. | Use one sensor replica or shared state. |
| `jira_sensor_for_apiv2.py` / trigger | Same sensor / `jira.issues_tracker_for_apiv2` | partial | Raw payload retained; scoped Attune Keys replace StackStorm config; name no longer implies a forced API version. | Verify target REST/search behavior. |
| Source tests and fixtures | `tests/test_pack.py` | adapted | StackStorm harness replaced by deterministic mocks and contract tests; no live Jira calls. | Add opt-in integration tests outside the pack gate. |
| Source CI/release metadata | Not migrated | manual | Repository publication policy belongs to the target GitHub repository. | Add target CI after repository creation. |
| Source workflows and rules | None | exact | No source resources existed. | Add operator-specific rules separately. |

## Runtime And Behavioral Boundaries

- Action timeout is passed to python-jira, but cancellation of an Attune execution may terminate the process rather than cancel an already transmitted Jira mutation.
- Actions add no Attune-level retry. The credential's explicit python-jira `max_retries` setting controls client retries.
- Mutating operations have Jira's native idempotency characteristics; no idempotency key is added.
- `bulk_link_issue` is the only concurrent action and uses at most ten threads sharing one Jira client.
- The polling sensor processes one page per rule tick in ascending issue ID order and advances only after successful event creation.
- No queue, workflow, compensation, schedule, or bundled event-to-action rule was invented because none existed upstream.

## Validation

```bash
attune --output json pack check .
attune pack test . --detailed
```

The deterministic test suite validates all 32 action metadata contracts,
authentication construction, TLS propagation, representative issue mutations,
partial bulk-link reporting, trigger/sensor linkage and Key-ref declarations,
baseline behavior, checkpoint ordering, output shapes, malformed stdin
handling, and secret absence. It does not contact Jira or prove worker runtime
installation.

## Upstream And License

This is a modified adaptation of the original
[StackStorm Exchange Jira pack](https://github.com/StackStorm-Exchange/stackstorm-jira).
The upstream Apache License 2.0 is included in [LICENSE](LICENSE), with
attribution details in [NOTICE](NOTICE).
