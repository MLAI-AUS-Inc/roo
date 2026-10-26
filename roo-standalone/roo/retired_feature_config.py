"""Remove retired feature configuration without widening a skill allowlist."""

import argparse
import os
from pathlib import Path
import re
import shlex
import tempfile


RETIRED_KEYS = frozenset({
    "VICTOR_AI_ROO_SIGNING_SECRET", "VICTOR_AI_SKILL_ENABLED",
    "VICTOR_AI_SLACK_CHANNEL_NAME", "VICTOR_AI_BACKEND_TIMEOUT_SECONDS",
    "OFFICE_MANAGER_ACTIONS_ENABLED",
})
ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def retire_feature_config(path: Path) -> None:
    """Atomically prune obsolete dotenv keys and the retired Victor skill."""
    path = Path(path)
    lines = path.read_text().splitlines(keepends=True)
    updated = []
    for line in lines:
        match = ENV_ASSIGNMENT.match(line)
        if match is None:
            updated.append(line)
            continue
        key, raw_value = match.groups()
        if key in RETIRED_KEYS:
            continue
        if key == "ROO_ENABLED_SKILLS":
            value = " ".join(shlex.split(raw_value, comments=True))
            skills = value.replace(",", " ").split()
            remaining = [name for name in skills if name != "victor-ai-applications"]
            if len(remaining) != len(skills):
                # Blank means built-in defaults. [] explicitly enables no skills.
                line = f"ROO_ENABLED_SKILLS={','.join(remaining) if remaining else '[]'}\n"
        updated.append(line)
    content = "".join(updated)
    if content == "".join(lines):
        return
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".env.retired.", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    retire_feature_config(parser.parse_args().path)
