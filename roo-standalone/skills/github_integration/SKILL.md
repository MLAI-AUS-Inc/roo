---
name: github-integration
description: Interact with the user's GitHub repositories (scan, analyze, access).
requires_auth: true
parameters:
  - repo_name: The name of the repository to scan (e.g. owner/project).
  - domain: The domain associated with the project (optional).
  - action: One of reconnect or scan.
routing:
  use_when: >
    The user's GitHub connection/auth is the subject: connecting or reconnecting
    GitHub, authorising access, fixing broken GitHub integration, linking an account.
  avoid_when: >
    Scanning a repo/domain to produce content (content-factory). Questions about
    code or git itself. "Connect me with someone" (connect-users).
  examples:
    - {text: "reconnect github for mlai.au", action: reconnect}
    - {text: "I need to reauthorise github", action: reconnect}
    - {text: "github access is broken for studynash.co, can you fix it?", action: reconnect}
    - {text: "link my github account", action: reconnect}
  negative_examples:
    - {text: "scan the repo for the domain mlai.au", instead: content-factory}
    - {text: "connect me with someone who writes blog content", instead: connect-users}
actions:
  - name: reconnect
    description: (Re)connect or re-authorise the user's GitHub integration.
    params:
      domain: {type: string, description: "Related domain if mentioned."}
      repo_name: {type: string, description: "owner/repo if mentioned."}
  - name: scan
    description: Scan a connected repository via the GitHub integration.
    params:
      domain: {type: string}
      repo_name: {type: string}
---

# GitHub Integration Skill
This skill allows Roo to perform actions on the user's GitHub repositories, such as scanning them to understand project structure for content generation.

## Instructions
1. **Manual reconnect**: If the user says `reconnect to github`, `authenticate with github`, or similar, start the existing GitHub auth flow for the active domain immediately.
2. **Resolve domain carefully**: Prefer an explicit domain from the prompt. Fall back to thread or connected-domain context. If multiple connected domains are plausible, ask which domain to reconnect instead of guessing.
3. **Check connection**: Always ensure the user has connected their GitHub account before attempting scan or publish-related actions. Use the reconnect flow when access is missing or expired.
4. **Scan repository**: If the user wants to `scan` or `analyze` a repo, use the scan action only after GitHub auth is healthy for that domain.
5. **Retry model**: After reconnecting, keep the pending action available and ask the user to retry explicitly. Do not silently auto-resume V1 flows.

## Parameters
- **repo_name**: The repository identifier in "owner/repo" format.
- **domain**: (Optional) The target domain name for the project (e.g. `mlai.au`).
- **action**: `reconnect` for manual auth repair, `scan` for repository analysis.
