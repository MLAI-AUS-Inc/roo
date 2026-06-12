"""
Skill Loader

Parses skill definitions from SKILL.md files in skill directories.
Follows Anthropic's Agent Skills pattern with progressive disclosure.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any
import importlib.util
import sys

import re
import frontmatter


@dataclass
class Skill:
    """
    A skill definition loaded from a SKILL.md file.

    Skills follow the Anthropic Agent Skills pattern:
    - Each skill is a directory containing SKILL.md
    - SKILL.md has YAML frontmatter (name, description) and markdown body
    - Optional implementation files (*.py) can be co-located
    """
    name: str
    description: str
    content: str  # Full markdown body with instructions
    path: Path  # Path to the skill directory
    trigger_keywords: List[str] = field(default_factory=list)
    requires_auth: bool = False
    parameters: List[dict] = field(default_factory=list)
    priority_channels: List[str] = field(default_factory=list)
    exclusive_channels: List[str] = field(default_factory=list)
    # Router v2 metadata (single source of truth for LLM routing):
    # routing: {use_when, avoid_when, examples: [{text, action?}],
    #           negative_examples: [{text, instead}]}
    routing: Dict[str, Any] = field(default_factory=dict)
    # actions: [{name, description, params: {param_name: {type, description}}}]
    actions: List[Dict[str, Any]] = field(default_factory=list)

    # Loaded implementation module (if any)
    _module: Optional[Any] = field(default=None, repr=False)

    def action_names(self) -> List[str]:
        return [str(action.get("name")) for action in self.actions if action.get("name")]
    
    def __repr__(self):
        return f"Skill(name='{self.name}', path='{self.path.name}')"
    
    def get_client_class(self, class_name: str = None):
        """
        Get a client class from the skill's implementation module.
        
        Args:
            class_name: Name of the class to get. If None, looks for *Client class.
        
        Returns:
            The client class, or None if not found.
        """
        if self._module is None:
            return None
        
        if class_name:
            return getattr(self._module, class_name, None)
        
        # Auto-detect *Client class
        for name in dir(self._module):
            if name.endswith("Client") and not name.startswith("_"):
                return getattr(self._module, name)
        
        return None


def load_skills(skills_dir: Path) -> List[Skill]:
    """
    Load all skill definitions from skill directories.
    
    Supports two structures:
    1. New (preferred): skills/skill_name/SKILL.md
    2. Legacy: skills/skill_name.md
    
    Args:
        skills_dir: Directory containing skill folders/files
    
    Returns:
        List of Skill objects
    """
    skills = []
    
    if not skills_dir.exists():
        print(f"⚠️  Skills directory not found: {skills_dir}")
        return skills
    
    # First, load from directories (new pattern)
    for item in skills_dir.iterdir():
        if item.is_dir() and not item.name.startswith(("_", ".")):
            skill_file = item / "SKILL.md"
            if skill_file.exists():
                try:
                    skill = load_skill_from_directory(item)
                    if skill:
                        skills.append(skill)
                        print(f"   ✅ Loaded skill: {skill.name} (from {item.name}/)")
                except Exception as e:
                    print(f"   ❌ Failed to load {item.name}/SKILL.md: {e}")
    
    # Then, load legacy flat files (for backwards compatibility)
    for md_file in skills_dir.glob("*.md"):
        # Skip if we already loaded this as a directory
        skill_name = md_file.stem
        if any(s.name == skill_name or s.name == skill_name.replace("_", "-") for s in skills):
            continue
        
        try:
            skill = load_skill_file(md_file)
            if skill:
                skills.append(skill)
                print(f"   ✅ Loaded skill: {skill.name} (legacy: {md_file.name})")
        except Exception as e:
            print(f"   ❌ Failed to load {md_file.name}: {e}")
    
    return skills


def load_skill_from_directory(skill_dir: Path) -> Optional[Skill]:
    """
    Load a skill from a directory containing SKILL.md.
    
    Also loads any Python implementation files in the directory.
    """
    skill_file = skill_dir / "SKILL.md"
    post = frontmatter.load(skill_file)
    
    name = post.metadata.get("name")
    if not name:
        print(f"   ⚠️  Skipping {skill_dir.name}: missing 'name' in frontmatter")
        return None
    
    # Extract parameters from markdown if not in frontmatter
    parameters = post.metadata.get("parameters", [])
    if not parameters and post.content:
        parameters = _extract_parameters_from_markdown(post.content)
    
    skill = Skill(
        name=name,
        description=post.metadata.get("description", ""),
        content=post.content,
        path=skill_dir,
        trigger_keywords=post.metadata.get("trigger_keywords", []),
        requires_auth=post.metadata.get("requires_auth", False),
        parameters=parameters,
        priority_channels=post.metadata.get("priority_channels", []),
        exclusive_channels=post.metadata.get("exclusive_channels", []),
        routing=_validate_routing(name, post.metadata.get("routing")),
        actions=_validate_actions(name, post.metadata.get("actions")),
    )

    # Load implementation module if present
    client_file = skill_dir / "client.py"
    if client_file.exists():
        try:
            skill._module = _load_module_from_file(client_file, f"skill_{name}_client")
            print(f"      📦 Loaded implementation: client.py")
        except Exception as e:
            print(f"      ⚠️  Failed to load client.py: {e}")
    
    return skill


def load_skill_file(file_path: Path) -> Optional[Skill]:
    """Load a single skill from a legacy flat markdown file."""
    post = frontmatter.load(file_path)
    
    name = post.metadata.get("name")
    if not name:
        print(f"   ⚠️  Skipping {file_path.name}: missing 'name' in frontmatter")
        return None
    
    # Try to extract parameters from markdown if not in frontmatter
    parameters = post.metadata.get("parameters", [])
    if not parameters and post.content:
        parameters = _extract_parameters_from_markdown(post.content)

    return Skill(
        name=name,
        description=post.metadata.get("description", ""),
        content=post.content,
        path=file_path.parent,
        trigger_keywords=post.metadata.get("trigger_keywords", []),
        requires_auth=post.metadata.get("requires_auth", False),
        parameters=parameters,
        priority_channels=post.metadata.get("priority_channels", []),
        exclusive_channels=post.metadata.get("exclusive_channels", []),
    )


def _validate_routing(skill_name: str, raw: Any) -> Dict[str, Any]:
    """Validate the SKILL.md `routing:` block; raise loudly on malformed data."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{skill_name}: routing must be a mapping, got {type(raw).__name__}")
    for key in ("use_when", "avoid_when"):
        if key in raw and not isinstance(raw[key], str):
            raise ValueError(f"{skill_name}: routing.{key} must be a string")
    for key in ("examples", "negative_examples"):
        entries = raw.get(key) or []
        if not isinstance(entries, list):
            raise ValueError(f"{skill_name}: routing.{key} must be a list")
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("text"):
                raise ValueError(
                    f"{skill_name}: every routing.{key} entry needs a 'text' field: {entry!r}"
                )
    return raw


def _validate_actions(skill_name: str, raw: Any) -> List[Dict[str, Any]]:
    """Validate the SKILL.md `actions:` block; raise loudly on malformed data."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{skill_name}: actions must be a list, got {type(raw).__name__}")
    seen = set()
    for action in raw:
        if not isinstance(action, dict) or not action.get("name"):
            raise ValueError(f"{skill_name}: every action needs a 'name': {action!r}")
        name = str(action["name"])
        if name in seen:
            raise ValueError(f"{skill_name}: duplicate action name '{name}'")
        seen.add(name)
        params = action.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError(f"{skill_name}: action '{name}' params must be a mapping")
    return raw


def _load_module_from_file(file_path: Path, module_name: str):
    """Dynamically load a Python module from a file path."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")
    
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _extract_parameters_from_markdown(content: str) -> List[dict]:
    """
    Extract parameters from '## Parameters' section in markdown.
    
    Expected format:
    - **name**: description
    """
    parameters = []
    
    # improved regex to capture parameter section
    # looks for ## Parameters, then captures lines until next heading or end
    match = re.search(r'##\s+Parameters\s*\n(.*?)(?=\n##|\Z)', content, re.DOTALL | re.IGNORECASE)
    
    if not match:
        return parameters
        
    section = match.group(1)
    
    # Parse bullet points
    for line in section.split('\n'):
        line = line.strip()
        if not line.startswith(('-', '*')):
            continue
            
        # Extract name and description
        # - **query**: The expertise...
        param_match = re.search(r'[-*]\s+\*\*([a-zA-Z0-9_]+)\*\*:\s*(.*)', line)
        if param_match:
            name, desc = param_match.groups()
            parameters.append({
                "name": name,
                "description": desc.strip(),
                "required": "(required)" in desc.lower(),
                "default": _extract_default(desc)
            })
            
    return parameters


def _extract_default(desc: str) -> Optional[str]:
    """Extract default value from description."""
    match = re.search(r'\(default:\s*(.*?)\)', desc, re.IGNORECASE)
    return match.group(1) if match else None
