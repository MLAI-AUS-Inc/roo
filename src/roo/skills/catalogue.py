"""Metadata-only catalogue validation: never imports a skill's Python files."""
from dataclasses import dataclass, field
from pathlib import Path
import re
import frontmatter
import yaml


@dataclass(frozen=True)
class Diagnostic:
    path: Path
    message: str
    severity: str = "error"


@dataclass
class Package:
    path: Path
    metadata: dict = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def name(self):
        value = self.metadata.get("name")
        return value if isinstance(value, str) else None


def inspect_package(path: Path) -> Package:
    package = Package(path)
    def error(message):
        package.diagnostics.append(Diagnostic(path, message))
    try:
        post = frontmatter.load(path)
        meta = package.metadata = dict(post.metadata)
        known_fields = {"name", "description", "requires_auth", "parameters",
                        "exclusive_channels", "priority_channels", "routing", "actions"}
        for key in sorted(meta.keys() - known_fields, key=str):
            package.diagnostics.append(Diagnostic(
                path, f"Unknown metadata field {key!r}; check for a spelling mistake", "warning"))
        routing_path = path.parent / "routing.yaml"
        if path.name == "SKILL.md" and routing_path.exists():
            if meta.get("routing"):
                error("routing: define either frontmatter or routing.yaml, not both")
            else:
                meta["routing"] = yaml.safe_load(routing_path.read_text()) or {}
        name = meta.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            error("name: expected a non-empty lowercase hyphenated identifier")
        if not isinstance(meta.get("description"), str) or not meta["description"].strip():
            error("description: expected a non-empty string")
        if "requires_auth" in meta and type(meta["requires_auth"]) is not bool:
            error("requires_auth: expected a boolean")
        for key in ("exclusive_channels", "priority_channels"):
            value = meta.get(key, [])
            if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
                error(f"{key}: expected a list of non-empty channel names")
        actions = meta.get("actions", [])
        names = set()
        if not isinstance(actions, list):
            error("actions: expected a list")
            actions = []
        for i, action in enumerate(actions):
            if not isinstance(action, dict) or not isinstance(action.get("name"), str) or not action["name"]:
                error(f"actions[{i}].name: expected a non-empty string")
                continue
            if action["name"] in names:
                error(f"actions[{i}].name: duplicate action {action['name']!r}")
            names.add(action["name"])
            params = action.get("params", {})
            if not isinstance(params, dict):
                error(f"actions[{i}].params: expected a mapping")
                continue
            for key, spec in params.items():
                if not isinstance(spec, dict):
                    error(f"actions[{i}].params.{key}: expected a mapping")
                    continue
                if spec.get("type", "string") not in {"string", "integer", "number", "boolean", "array", "object"}:
                    error(f"actions[{i}].params.{key}.type: unsupported JSON schema type")
                if "enum" in spec and not isinstance(spec["enum"], list):
                    error(f"actions[{i}].params.{key}.enum: expected a list")
        routing = meta.get("routing", {})
        if not isinstance(routing, dict):
            error("routing: expected a mapping")
            routing = {}
        for key in ("use_when", "avoid_when"):
            if key in routing and not isinstance(routing[key], str):
                error(f"routing.{key}: expected a string")
        if "event_only" in routing and type(routing["event_only"]) is not bool:
            error("routing.event_only: expected a boolean")
        for key in ("examples", "negative_examples"):
            examples = routing.get(key, [])
            if not isinstance(examples, list):
                error(f"routing.{key}: expected a list")
                continue
            for i, example in enumerate(examples):
                if not isinstance(example, dict) or not isinstance(example.get("text"), str) or not example["text"]:
                    error(f"routing.{key}[{i}].text: expected a non-empty string")
                elif key == "examples" and example.get("action") is not None and example["action"] not in names:
                    error(f"routing.examples[{i}].action: undeclared action {example['action']!r}")
    except Exception as exc:
        error(f"Cannot parse metadata ({type(exc).__name__}): {exc}")
    return package


def inspect_catalogue(root: Path) -> list[Package]:
    if not root.is_dir():
        return [Package(root, diagnostics=[Diagnostic(root, "Skill directory does not exist")])]
    paths = sorted(p / "SKILL.md" for p in root.iterdir()
                   if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "SKILL.md").is_file())
    packages = [inspect_package(p) for p in paths + sorted(root.glob("*.md"))]
    by_name = {}
    for package in packages:
        if package.name:
            by_name.setdefault(package.name, []).append(package)
    for name, matches in by_name.items():
        if len(matches) > 1:
            for package in matches:
                package.diagnostics.append(Diagnostic(package.path, f"Duplicate skill name {name!r}"))
    return packages
