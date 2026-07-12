"""AI-first tool agents for Dr Snow and Nurse Paws.

The model always receives the player's message first. Clinical facts remain in
local code and are exposed only through allowlisted tools backed by cases.yaml.
No tool in this module can adjudicate or persist a diagnosis guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .config import get_settings
from .llm import get_llm_client


DR_SNOW_NAME = "Dr Snow"
NURSE_PAWS_NAME = "Nurse Paws"


DR_SNOW_PROMPT = """You are Dr Snow, the radiology registrar at the hospital results desk. You can access both pathology and radiology results for the patient in cubicle 3.

You are the first conversational responder for every message. For greetings or ordinary conversation, answer directly. Whenever you state a clinical result, you MUST obtain it from one of your tools in this turn or from the supplied prior conversation. Never invent, normalize, round, or assume an unperformed result.

Use list_available_results when the player asks what is available. Use get_results for requested bloods, pathology, ECG, gases, urine, endocrine tests, or imaging. If the player explicitly says they are stuck, seems lost after repeated vague questions, or asks for a useful clue, you may call offer_imaging_clue and share one real scan that has not already been discussed.

You do not know the hidden diagnosis and must never confirm or reject a proposed diagnosis. Nurse Paws takes final diagnoses. Redirect requests for observations or physical examination to Nurse Paws, and history or symptoms to the patient.

Speak warmly and efficiently in one or two short paragraphs. Return only Dr Snow's spoken words: no labels, narration, stage directions, markdown, JSON, or tool names."""


NURSE_PAWS_PROMPT = """You are Nurse Paws, the senior ward nurse at reception. You help the player synthesize the case, provide the complete observations and physical examination, and prepare their ONE official diagnosis for explicit confirmation.

You are the first conversational responder for every message. Clinical observations and examination findings MUST come from your tools in this turn or from the supplied prior conversation. Never invent or round a finding. You may reason with the player about patterns and differentials using only facts already disclosed, but you do not know the hidden diagnosis or acceptable answers and must never say whether a proposed diagnosis is correct.

Use get_observations for vital signs and get_examination for physical findings. Call prepare_final_guess only when the player clearly declares a final diagnosis or explicitly asks to submit it. Never call it for tentative language such as "could it be" or "what about". prepare_final_guess does not submit anything; after it succeeds, tell the player to review and press the confirmation button. If the contest is not eligible, do not invite another submission.

Speak like a warm, quick, slightly playful senior nurse in one or two short paragraphs. Return only Nurse Paws' spoken words: no labels, narration, stage directions, markdown, JSON, or tool names."""


@dataclass
class WardAgentResult:
    reply: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    suggested_action: Optional[dict[str, str]] = None


def _strict_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "strict": True,
    }


def _human_label(identifier: str) -> str:
    return identifier.replace("_", " ").replace(".", " · ").strip().title()


_NON_RESULT_ROOTS = {
    "exposure_details_if_asked_calmly",
    "medication_clue_if_asked_properly",
    "medication_history_reveals",
    "substance_history_if_asked_nonjudgmentally",
}
_RADIOLOGY_MARKERS = {
    "ct",
    "cxr",
    "imaging",
    "mri",
    "mrv",
    "radiograph",
    "ultrasound",
    "venogram",
    "xray",
}


def _investigation_category(path: tuple[str, ...]) -> str:
    tokens = {
        token
        for segment in path
        for token in segment.lower().replace("-", "_").split("_")
    }
    return "radiology" if tokens & _RADIOLOGY_MARKERS else "pathology"


def investigation_catalog(case: dict) -> dict[str, dict[str, Any]]:
    """Flatten every authored clinical test into stable, non-secret result ids.

    Case files use both common blocks (``bloods`` and ``imaging_if_ordered``)
    and case-specific blocks such as ``confirmatory_test`` or
    ``key_diagnostic_test``. Recursing prevents those authored results from
    silently disappearing. Narrative medication/exposure clues remain with the
    patient, and author notes are instructions rather than reportable results.
    """
    investigations = case.get("investigations") or {}
    catalog: dict[str, dict[str, Any]] = {}

    def visit(value: Any, path: tuple[str, ...]) -> None:
        if not path or path[0] in _NON_RESULT_ROOTS or path[-1] == "note":
            return
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, (*path, str(key)))
            return
        if value in (None, ""):
            return
        identifier = ".".join(path)
        catalog[identifier] = {
            "label": _human_label(identifier),
            "category": _investigation_category(path),
            "value": value,
        }

    if isinstance(investigations, dict):
        for key, value in investigations.items():
            visit(value, (str(key),))
    return catalog


def _dr_snow_tools(case: dict) -> list[dict[str, Any]]:
    ids = sorted(investigation_catalog(case))
    selectable_ids = ids + ["bloods", "pathology", "radiology", "imaging", "all"]
    return [
        _strict_tool(
            "list_available_results",
            "List the authored pathology and/or radiology results available for this patient without revealing their values.",
            {
                "category": {
                    "type": "string",
                    "enum": ["pathology", "radiology", "all"],
                    "description": "Which result category to list.",
                },
            },
            ["category"],
        ),
        _strict_tool(
            "get_results",
            "Retrieve exact authored result values. Use bloods/pathology/radiology/imaging/all to retrieve a complete group.",
            {
                "test_ids": {
                    "type": "array",
                    "items": {"type": "string", "enum": selectable_ids},
                    "minItems": 1,
                    "maxItems": 20,
                    "description": "Stable test ids or group aliases to retrieve.",
                },
            },
            ["test_ids"],
        ),
        _strict_tool(
            "offer_imaging_clue",
            "Return one real, not-yet-discussed authored scan when the player appears to be struggling. Do not use merely to answer a normal specific request.",
            {},
            [],
        ),
    ]


def _paws_tools(case: dict) -> list[dict[str, Any]]:
    systems = sorted((case.get("examination") or {}).keys())
    return [
        _strict_tool(
            "get_observations",
            "Retrieve the complete authored vital signs and observations for the patient.",
            {},
            [],
        ),
        _strict_tool(
            "get_examination",
            "Retrieve exact authored physical examination findings for one system or the complete examination.",
            {
                "system": {
                    "type": "string",
                    "enum": ["all", *systems],
                    "description": "The examination system to retrieve, or all.",
                },
            },
            ["system"],
        ),
        _strict_tool(
            "prepare_final_guess",
            "Prepare an explicitly final diagnosis for a separate player confirmation. This never submits, adjudicates, or burns the attempt.",
            {
                "diagnosis": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "The diagnosis the player explicitly declared as final.",
                },
            },
            ["diagnosis"],
        ),
    ]


def _execute_dr_snow_tool(
    name: str,
    arguments: dict[str, Any],
    case: dict,
    history: list[dict],
) -> dict[str, Any]:
    catalog = investigation_catalog(case)
    if name == "list_available_results":
        category = arguments.get("category")
        results = [
            {"id": identifier, "label": item["label"], "category": item["category"]}
            for identifier, item in catalog.items()
            if category == "all" or item["category"] == category
        ]
        return {"results": results}

    if name == "get_results":
        requested = arguments.get("test_ids") or []
        expanded: list[str] = []
        for identifier in requested:
            if identifier in {"bloods"}:
                expanded.extend(key for key in catalog if key.startswith("bloods."))
            elif identifier in {"pathology", "radiology", "imaging", "all"}:
                target = "radiology" if identifier in {"radiology", "imaging"} else identifier
                expanded.extend(
                    key for key, item in catalog.items()
                    if target == "all" or item["category"] == target
                )
            else:
                expanded.append(identifier)
        unique = list(dict.fromkeys(expanded))[:30]
        return {
            "results": [
                {
                    "id": identifier,
                    "label": catalog[identifier]["label"],
                    "value": catalog[identifier]["value"],
                }
                for identifier in unique
                if identifier in catalog
            ],
            "unavailable": [identifier for identifier in unique if identifier not in catalog],
        }

    if name == "offer_imaging_clue":
        transcript = " ".join(
            str(turn.get("text") or "") for turn in history if isinstance(turn, dict)
        ).lower()
        for identifier, item in catalog.items():
            if item["category"] != "radiology":
                continue
            label = str(item["label"])
            if identifier.lower() in transcript or label.lower() in transcript:
                continue
            return {
                "available": True,
                "result": {
                    "id": identifier,
                    "label": label,
                    "value": item["value"],
                },
            }
        return {"available": False, "reason": "No undisclosed authored scan remains."}

    return {"error": f"Unknown Dr Snow tool: {name}"}


def _execute_paws_tool(
    name: str,
    arguments: dict[str, Any],
    case: dict,
    contest_state: dict[str, Any],
    action_holder: dict[str, Any],
) -> dict[str, Any]:
    if name == "get_observations":
        return {"observations": case.get("vitals") or {}}

    if name == "get_examination":
        examination = case.get("examination") or {}
        system = arguments.get("system")
        if system == "all":
            return {"examination": examination}
        if system not in examination:
            return {"available": False, "system": system}
        return {"examination": {system: examination[system]}}

    if name == "prepare_final_guess":
        diagnosis = str(arguments.get("diagnosis") or "").strip()[:200]
        if contest_state.get("state") != "eligible":
            return {
                "prepared": False,
                "contest_state": contest_state.get("state", "unavailable"),
                "reason": "The one-shot contest is not eligible for a new submission.",
            }
        if not diagnosis:
            return {"prepared": False, "reason": "No diagnosis was supplied."}
        action_holder["value"] = {
            "type": "confirm_diagnosis",
            "diagnosis": diagnosis,
        }
        return {
            "prepared": True,
            "diagnosis": diagnosis,
            "instruction": "Ask the player to review and press the confirmation button.",
        }

    return {"error": f"Unknown Nurse Paws tool: {name}"}


def _format_history(history: list[dict], npc_name: str) -> str:
    if not history:
        return "None"
    lines = []
    for turn in history[-12:]:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        speaker = "Player" if turn.get("role") == "player" else npc_name
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines) if lines else "None"


async def run_ward_agent(
    *,
    role: str,
    question: str,
    history: list[dict],
    case: dict,
    contest_state: Optional[dict[str, Any]] = None,
) -> WardAgentResult:
    """Run one AI-first Dr Snow or Nurse Paws turn."""
    settings = get_settings()
    client = get_llm_client("openai")
    action_holder: dict[str, Any] = {}

    if role == "nurse":
        npc_name = DR_SNOW_NAME
        system_prompt = DR_SNOW_PROMPT
        tools = _dr_snow_tools(case)

        def execute(name: str, arguments: dict[str, Any]):
            return _execute_dr_snow_tool(name, arguments, case, history)
    elif role == "clerk":
        npc_name = NURSE_PAWS_NAME
        state = contest_state or {"state": "unavailable", "outcome": None}
        system_prompt = (
            NURSE_PAWS_PROMPT
            + f"\n\nAuthoritative contest state for this player: {state.get('state')}."
        )
        tools = _paws_tools(case)

        def execute(name: str, arguments: dict[str, Any]):
            return _execute_paws_tool(name, arguments, case, state, action_holder)
    else:
        raise ValueError(f"unsupported ward agent role: {role}")

    prompt = f"""Previous conversation with {npc_name}:
{_format_history(history, npc_name)}

Player's latest message: {question}

Respond in character. Use tools whenever clinical facts or a final-guess action are required."""
    response = await client.agent_with_tools(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        tools,
        execute,
        model=settings.SIM_PATIENT_MODEL,
        reasoning_effort=settings.SIM_PATIENT_REASONING_EFFORT,
        max_tokens=700,
        max_tool_rounds=2,
        tool_choice="auto",
    )
    return WardAgentResult(
        reply=response.content,
        model=response.model,
        usage=response.usage or {},
        tool_calls=response.tool_calls,
        suggested_action=action_holder.get("value"),
    )
