# Incorporate this into Roo

Two parts: a kickoff prompt to import everything, and config files (.roomodes + .roo/rules/)
that make Roo permanently aware of the project and its guardrails.

## Make it permanent
- .roomodes defines a dedicated mode, "🧾 MLAI Reconciler". Select it from Roo's mode dropdown.
- .roo/rules/00-mlai-reconciliation.md is loaded into the system prompt for every mode, so the
  guardrails apply even in normal Code mode.

## Setup steps
1. Open this folder as the workspace where Roo runs.
2. Roo auto-detects .roomodes and .roo/rules/. Reload the window if it was already open.
3. Pick "🧾 MLAI Reconciler" from the mode selector.
4. For the first run, paste the kickoff prompt. After that, say e.g. "reconcile June 2026".
5. Add .env to .gitignore (the .env.example is safe to commit).

## If "Roo" is your own agent (not Roo Code)
Put .roo/rules/00-mlai-reconciliation.md into your agent's system prompt, and use the kickoff
prompt as the first user message. Both pieces are framework-agnostic.
