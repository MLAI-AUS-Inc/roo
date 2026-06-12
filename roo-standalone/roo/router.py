"""Router v2 — one LLM tool-calling pass over the SKILL.md catalog.

Each skill becomes a tool whose description is generated from its SKILL.md
`routing:` block (use_when / avoid_when / examples / negative_examples) and
whose parameters are the skill's `actions:` enum plus the union of per-action
params. Two extra tools make "no skill" a first-class choice:

- respond_in_chat   general conversation / Q&A — no skill applies
- ask_clarification genuinely ambiguous between skills — ask one short question

Trust boundary: the system prompt contains only bot-generated metadata
(rules, channel, date, thread hint). Everything user-controlled (thread
history, file names, the message itself) goes in the user role.

Failure posture: invalid/unparsable tool calls are retried once with the
validation error appended; a second failure (or any transport error) returns
RouteDecision(skill=None, source="error") and the caller falls back to v1
behaviour / general chat. The fast path and delegation parsing stay in front
of this router permanently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .llm import ToolCallParseError, chat_tools
from .skills.loader import Skill

RESPOND_IN_CHAT = "respond_in_chat"
ASK_CLARIFICATION = "ask_clarification"

ROUTER_SYSTEM_PROMPT = """You are the skill router for Roo, the MLAI community Slack bot.
Pick exactly ONE tool for the user's message.

Rules:
- Route on what the user wants DONE, not on vocabulary. A message containing \
"article", "task" or "csv" is not automatically a skill request.
- Use respond_in_chat for general conversation, opinions, summaries of shared \
content, translations, explanations, and anything no skill is clearly for.
- Use ask_clarification ONLY when the message clearly needs a skill but you \
cannot choose between two specific skills. If the skill is clear and only a \
detail is missing, pick the skill anyway — skills ask for missing details \
themselves.
- When torn between a skill and respond_in_chat, prefer respond_in_chat.
- Fill parameters only with values actually present in the message or context; \
never invent values.
- Honour each tool's "Do NOT use" notes."""


@dataclass
class RouteDecision:
    """Outcome of one routing pass."""
    skill: Optional[str]                  # None => no skill (chat / clarification / error)
    action: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    source: str = "router"                # "router" | "error"
    clarification: Optional[str] = None   # set when the router asks a question

    @property
    def is_clarification(self) -> bool:
        return self.clarification is not None


def skill_available_in_channel(skill: Skill, channel_name: Optional[str]) -> bool:
    """Exclusive-channel skills are not candidates outside their channels.

    Unknown channel (None) is allowed through, mirroring the executor rule.
    """
    if not skill.exclusive_channels:
        return True
    if not channel_name:
        return True
    return channel_name in skill.exclusive_channels


def _tool_description(skill: Skill) -> str:
    routing = skill.routing or {}
    parts: List[str] = [skill.description.strip()] if skill.description else []
    use_when = str(routing.get("use_when") or "").strip()
    if use_when:
        parts.append(f"Use when: {use_when}")
    avoid_when = str(routing.get("avoid_when") or "").strip()
    if avoid_when:
        parts.append(f"Do NOT use when: {avoid_when}")
    examples = routing.get("examples") or []
    if examples:
        lines = []
        for example in examples:
            action = example.get("action")
            suffix = f" (action: {action})" if action else ""
            lines.append(f'- "{example["text"]}"{suffix}')
        parts.append("Examples:\n" + "\n".join(lines))
    negatives = routing.get("negative_examples") or []
    if negatives:
        lines = [
            f'- "{example["text"]}" -> use {example.get("instead", RESPOND_IN_CHAT)}'
            for example in negatives
        ]
        parts.append("Counter-examples (pick the other tool):\n" + "\n".join(lines))
    return "\n".join(parts)


def _action_param_properties(skill: Skill) -> Dict[str, Dict[str, Any]]:
    """Union of all actions' params, each optional. Types pass through as declared."""
    properties: Dict[str, Dict[str, Any]] = {}
    for action in skill.actions:
        for param_name, spec in (action.get("params") or {}).items():
            if param_name in properties:
                continue
            spec = dict(spec or {})
            prop: Dict[str, Any] = {"type": spec.get("type", "string")}
            if spec.get("enum"):
                prop["enum"] = spec["enum"]
            if spec.get("description"):
                prop["description"] = spec["description"]
            properties[param_name] = prop
    return properties


def _skill_tool(skill: Skill) -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    required: List[str] = []
    action_names = skill.action_names()
    if action_names:
        action_lines = [
            f"{action['name']}: {action.get('description', '')}".strip()
            for action in skill.actions
        ]
        properties["action"] = {
            "type": "string",
            "enum": action_names,
            "description": "What to do:\n" + "\n".join(action_lines),
        }
        required.append("action")
        properties.update(_action_param_properties(skill))

    return {
        "type": "function",
        "function": {
            "name": skill.name,
            "description": _tool_description(skill),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _chat_tool() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": RESPOND_IN_CHAT,
            "description": (
                "No skill applies — answer conversationally. Use for chit-chat, "
                "opinions, summaries/reviews of content the user shared, "
                "translations, explanations, general questions about events or "
                "the community, and anything that is not clearly a skill request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "One short phrase: why no skill applies.",
                    }
                },
                "required": [],
            },
        },
    }


def _clarification_tool() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": ASK_CLARIFICATION,
            "description": (
                "The message clearly needs a skill but is AMBIGUOUS BETWEEN TWO "
                "SPECIFIC SKILLS. Do NOT use this for missing details (a date, a "
                "title, a body…) — pick the skill instead; skills ask for their "
                "own missing details. Ask ONE short question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The single short question to ask the user.",
                    }
                },
                "required": ["question"],
            },
        },
    }


def build_tools(
    skills: List[Skill],
    channel_name: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Skill]]:
    """Build the tool list (channel-filtered) and a name->Skill map."""
    available = [skill for skill in skills if skill_available_in_channel(skill, channel_name)]
    tools = [_skill_tool(skill) for skill in available]
    tools.append(_chat_tool())
    tools.append(_clarification_tool())
    return tools, {skill.name: skill for skill in available}


def _context_lines(
    *,
    channel_name: Optional[str],
    skills_by_name: Dict[str, Skill],
    thread_hint: Optional[Dict[str, Any]],
    current_date: Optional[str],
) -> str:
    lines: List[str] = []
    if current_date:
        lines.append(f"Today's date: {current_date}")
    if channel_name:
        lines.append(f"Channel: #{channel_name}")
        for skill in skills_by_name.values():
            if channel_name in skill.priority_channels:
                lines.append(
                    f"This is the home channel of the '{skill.name}' skill — prefer it "
                    f"when the message plausibly relates to it."
                )
                break
    if thread_hint:
        hint_bits = []
        if thread_hint.get("skill_name"):
            hint_bits.append(f"last skill used: {thread_hint['skill_name']}")
        if thread_hint.get("domain"):
            hint_bits.append(f"domain: {thread_hint['domain']}")
        if thread_hint.get("workflow"):
            hint_bits.append(f"workflow: {thread_hint['workflow']}")
        if thread_hint.get("active_job_id"):
            hint_bits.append(f"active job: {thread_hint['active_job_id']}")
        if hint_bits:
            lines.append(
                "Thread context (this message is a reply in an ongoing thread; "
                "short follow-ups like 'do it' usually continue it): "
                + ", ".join(hint_bits)
            )
    return "\n".join(lines)


def _user_content(
    text: str,
    thread_history: Optional[List[dict]],
    file_names: Optional[List[str]],
) -> str:
    blocks: List[str] = []
    if thread_history:
        turns = [
            f"{message.get('user')}: {message.get('text')}"
            for message in thread_history[:-1][-6:]
            if message.get("text")
        ]
        if turns:
            blocks.append("Recent thread messages:\n<<<\n" + "\n".join(turns) + "\n>>>")
    if file_names:
        blocks.append("Attached files: " + ", ".join(str(name) for name in file_names))
    blocks.append(f"Message: {text}")
    return "\n\n".join(blocks)


def _validate_tool_call(
    name: str,
    arguments: Dict[str, Any],
    skills_by_name: Dict[str, Skill],
    *,
    strict_action: bool,
) -> RouteDecision:
    """Turn a raw tool call into a RouteDecision; raise ValueError to trigger a retry."""
    if name == RESPOND_IN_CHAT:
        return RouteDecision(skill=None, reason=str(arguments.get("reason") or ""))

    if name == ASK_CLARIFICATION:
        question = str(arguments.get("question") or "").strip()
        if not question:
            raise ValueError("ask_clarification requires a non-empty 'question'")
        return RouteDecision(skill=None, clarification=question, reason="clarification")

    skill = skills_by_name.get(name)
    if skill is None:
        raise ValueError(
            f"unknown tool '{name}'; valid: {sorted(skills_by_name)} + "
            f"['{RESPOND_IN_CHAT}', '{ASK_CLARIFICATION}']"
        )

    action = arguments.get("action")
    action_names = skill.action_names()
    if action_names:
        if action not in action_names:
            if strict_action:
                raise ValueError(
                    f"invalid action {action!r} for {skill.name}; valid: {action_names}"
                )
            action = None  # degrade: keep the skill, drop the bad action
    else:
        action = None

    declared = set(_action_param_properties(skill))
    params = {
        key: value
        for key, value in arguments.items()
        if key != "action" and key in declared and value not in (None, "", [])
    }
    return RouteDecision(skill=skill.name, action=action, params=params)


def _current_date_string() -> Optional[str]:
    try:
        from .utils import get_current_date

        return get_current_date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


async def route(
    text: str,
    *,
    skills: List[Skill],
    channel_name: Optional[str] = None,
    thread_history: Optional[List[dict]] = None,
    thread_hint: Optional[Dict[str, Any]] = None,
    file_names: Optional[List[str]] = None,
    model: Optional[str] = None,
    reasoning_effort: str = "medium",
) -> RouteDecision:
    """One routing pass. Never raises — errors come back as source="error"."""
    tools, skills_by_name = build_tools(skills, channel_name)

    context = _context_lines(
        channel_name=channel_name,
        skills_by_name=skills_by_name,
        thread_hint=thread_hint,
        current_date=_current_date_string(),
    )
    system = ROUTER_SYSTEM_PROMPT + ("\n\n" + context if context else "")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _user_content(text, thread_history, file_names)},
    ]

    if model is None:
        try:
            from .config import get_settings

            model = get_settings().ROUTER_MODEL
        except Exception:
            model = None

    kwargs: Dict[str, Any] = {"tool_choice": "required", "reasoning_effort": reasoning_effort}
    if model:
        kwargs["model"] = model

    last_error: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            call = await chat_tools(messages, tools, **kwargs)
            return _validate_tool_call(
                call.name,
                call.arguments,
                skills_by_name,
                strict_action=(attempt == 1),
            )
        except (ToolCallParseError, ValueError) as exc:
            last_error = exc
            messages = messages + [
                {
                    "role": "system",
                    "content": (
                        f"Your previous tool call was invalid: {exc}. "
                        "Answer again with one valid tool call."
                    ),
                }
            ]
        except Exception as exc:  # transport/provider errors: no retry loop here
            print(f"❌ Router v2 call failed: {exc}")
            return RouteDecision(skill=None, source="error", reason=str(exc))

    print(f"❌ Router v2 produced no valid tool call after retry: {last_error}")
    return RouteDecision(skill=None, source="error", reason=str(last_error))


def lint_catalog(skills: List[Skill]) -> List[str]:
    """Catalog hygiene checks. Returns a list of problems (empty = clean).

    Hard rules: routing block present, >=3 examples, >=1 negative example,
    no example text claimed by two different skills.
    """
    problems: List[str] = []
    claimed: Dict[str, str] = {}
    for skill in skills:
        routing = skill.routing or {}
        if not routing:
            problems.append(f"{skill.name}: missing routing block in SKILL.md")
            continue
        if not str(routing.get("use_when") or "").strip():
            problems.append(f"{skill.name}: routing.use_when is empty")
        examples = routing.get("examples") or []
        if len(examples) < 3:
            problems.append(f"{skill.name}: needs >=3 routing examples (has {len(examples)})")
        if len(routing.get("negative_examples") or []) < 1:
            problems.append(f"{skill.name}: needs >=1 negative example")
        action_names = set(skill.action_names())
        for example in examples:
            text_lower = str(example.get("text") or "").strip().lower()
            if text_lower in claimed and claimed[text_lower] != skill.name:
                problems.append(
                    f"example {example.get('text')!r} claimed by both "
                    f"{claimed[text_lower]} and {skill.name}"
                )
            claimed.setdefault(text_lower, skill.name)
            example_action = example.get("action")
            if example_action and action_names and example_action not in action_names:
                problems.append(
                    f"{skill.name}: example action {example_action!r} not in actions"
                )
    return problems
