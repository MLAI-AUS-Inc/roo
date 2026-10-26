# Developing and validating Roo skills

Run tooling from `roo-standalone` with the application's Python environment:

```bash
python -m roo.skills.lint --public-defaults
python -m roo.skills.lint --required admin-brain admin-actions
python -m pytest ../tests/roo/test_skill_loading_contract.py -q
python -m pytest ../tests/roo/test_skill_execution_behaviors.py ../tests/roo/test_skill_executor_classes.py -q
```

The linter reads frontmatter and optional `routing.yaml`; it does not import
skill clients, initialize the agent, read runtime `.env`, or call services.
`--public-defaults` checks the default public skill set. For an explicit
deployment allowlist, pass those names with `--required`; feature-flag-added
skills must also be named there. The runtime independently checks its complete
effective allowlist, including feature-flag additions.

Errors include malformed metadata, incorrect field types, duplicate skill or
action names, examples referencing undeclared actions, missing required packages,
and missing required client files. Errors exit with status 1. Unknown metadata
fields produce spelling-check warnings without disabling the skill. Validation
does not prove user authorization, backend availability, or successful client
imports; those remain runtime concerns. CI runs the metadata linter and the
isolated loading/dispatch contract suite before its existing checks. Executor
registration and interface compliance are checked separately by runtime startup
and registry tests; metadata lint never imports the runtime registry.

## Adding a capability

1. Add `roo-standalone/skills/<package>/SKILL.md` with a unique `name`, `description`,
   routing rules, and declared actions/parameters. Use actual YAML booleans for
   `requires_auth`. It is descriptive metadata, not an authorization grant.
2. Implement a class under `roo/skills/executors/` inheriting
   `SkillExecutorInterface` from `roo.skills.contracts`, with
   `async execute(request: SkillRequest)`. Import the class normally and add
   `"skill-name": YourExecutor` to `EXECUTORS` in `roo/skills/registry.py`.
   Each coordinator owns executor
   instances; there are no global mutable executor instances. The request carries
   the skill, text, parameters, actor/channel/thread, history, and event metadata.
   Retain backend authorization and write gates inside the owning domain.
   If the executor consumes a package client class, declare it in
   `catalogue.REQUIRED_CLIENTS`: lint checks that `client.py` exists, and runtime loading
   verifies that it exports the required class.
3. Add it to the appropriate deployment configuration only after reviewing
   Public/Admin ownership. Registration does not enable a skill or grant access.
4. Add offline executor, denied-action, and routing tests. Run the linter and
   affected suites separately where import stubs require isolation.

There is no implicit generic-chat fallback for unregistered skills. Text-only
skills also need an explicit executor. Event-driven capabilities are explicitly
registered in `EVENT_EXECUTORS` (which defines `EVENT_ONLY_SKILLS`) and continue
to execute through their dedicated ingress; that list does not grant execution
through chat. StartHereIntroductionsExecutor accepts a Slack event, rather than
pretending it is a chat request.

## Executor structure

`SkillExecutorInterface` is an abstract base class requiring
`async execute(request: SkillRequest) -> str | dict[str, Any]`. Registered
classes inherit it and implement an async method. Abstract classes cannot be
instantiated; registry tests check inheritance and asynchronous execution for
every entry. `EventSkillExecutorInterface` defines the separate event contract.
Typed class maps and tests keep event and chat executors separate. There are no
binding objects, string-based class lookups, or custom executor factories.

`SkillExecutor` dispatches requests and normalizes results. All domain execution
methods now live in their owning classes. Existing Slack callbacks can still
access legacy helper names through the coordinator's forwarding interface;
executors never call back into that coordinator. New callers should use
`executor.execute(request)` instead of private helper methods.

`PromptExecutor` supplies shared prompt execution with injectable `chat_fn` and
`client_factory` dependencies. `ToneOfVoiceExecutor` uses it with the existing
tone prompt and model options; `ConnectUsersExecutor` uses the generic skill
instructions prompt. Watt-the-hack and points retain their existing explicit
prompt fallback paths. This does not introduce fallback for unknown skills.

Publishing and Linear reading helpers remain shared in `PublishingSupport`
and `LinearReadSupport`; backend error formatting is in `BackendErrors`.
Large domains such as points and Linear meeting actions still have substantial
internal workflows, but no longer share one executor class with unrelated skills.

The [skill coverage and refactor report](skill-executor-validation.md) maps all
20 skill packages to their executors and regression suites.

## Loading and failures

`load_skill_report(path, allowed_names=...)` returns `SkillLoadReport` with:

| Entry status | Meaning |
| --- | --- |
| `loaded` | Valid metadata and successful client import when requested |
| `disabled` | Named package excluded by the deployment allowlist; client not imported |
| `broken` | Invalid/duplicate metadata or client import failure; excluded from usable skills |
| `missing` | Requested name has no discovered package |

Entries include paths, error messages, and warnings for loaded packages.
`report.skills` contains usable definitions; `report.require(names)` raises on
missing/broken required names. `load_skills` remains the compatibility list API.

RooAgent retains `skill_load_report` and requires every effectively enabled skill,
whether selected by defaults, feature flags, or an explicit allowlist. This is
stricter than the previous behavior: a deployment missing a default package now
fails at startup instead of silently advertising a smaller set. Duplicate names
are rejected, including directory/legacy-flat collisions. Failed client imports
no longer leave the skill available. Startup also checks execution bindings.

Admin routing metadata uses `import_clients=False`; this validates definitions
without loading administrative client code in Public Roo. This mode is not an
execution-readiness check. Public/Admin credential and permission checks remain
unchanged. No schema changes or live dependency probes are introduced.
