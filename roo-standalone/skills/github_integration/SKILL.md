---
name: github-integration
description: Interact with the user's GitHub repositories (scan, analyze, access).
trigger_keywords:
  - scan repo
  - connect github
  - analyze repo
  - github integration
requires_auth: true
parameters:
  - **repo_name**: The name of the repository to scan (e.g. owner/project).
  - **domain**: The domain associated with the project (optional).
---

# GitHub Integration Skill
This skill allows Roo to perform actions on the user's GitHub repositories, such as scanning them to understand project structure for content generation.

## Instructions
1.  **Check Connection**: Always ensure the user has connected their GitHub account before attempting any actions. Use the provided tools to check and request connection if needed.
2.  **Scan Repository**: If the user wants to "scan" or "analyze" a repo, use the `scan_repo` action. This will trigger the Content Factory's analysis pipeline.
3.  **Context**: When scanning, if a domain is provided, pass it along.

## Parameters
- **repo_name**: The repository identifier in "owner/repo" format.
- **domain**: (Optional) The target domain name for the project (e.g. `mlai.au`).
