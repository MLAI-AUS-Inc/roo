---
name: connect-users
description: Find community members with relevant expertise using vector search
routing:
  use_when: >
    The user wants to FIND, MEET, or BE INTRODUCED to community members by
    expertise, interest, or background: "anyone who…", "who knows…", "connect
    me with…", "looking for a mentor/teammate/cofounder in…".
  avoid_when: >
    Connecting SERVICES or integrations (github-integration). Questions about a
    topic itself rather than about people ("explain X" is chat).
  examples:
    - {text: "do you know anyone in AI research?", action: search}
    - {text: "anyone in the community working with medical imaging?", action: search}
    - {text: "connect me with someone who writes blog content", action: search}
    - {text: "my ecg project needs a teammate, anyone interested in medical AI?", action: search}
    - {text: "looking for a mentor in data engineering, any suggestions?", action: search}
  negative_examples:
    - {text: "connect github again for mlai.au", instead: github-integration}
    - {text: "explain what a vector database is", instead: respond_in_chat}
actions:
  - name: search
    description: Search the community for members matching an expertise/interest query.
    params:
      query: {type: string, description: "The expertise or topic to search for."}
      limit: {type: integer, description: "Max suggestions (default 5)."}
---

# Connect Users Skill

This skill enables Claude to find and recommend MLAI community members based on their expertise, interests, and what they're working on.

## Capabilities

- Search for users with specific expertise using vector similarity
- Match users based on topics, skills, and interests
- Provide warm introductions and connection suggestions

## Parameters

- **query**: The expertise or topic the user is looking for (required)
- **exclude_user_id**: Slack user ID to exclude from results (optional - usually the requester)
- **limit**: Maximum number of users to suggest (default: 5)

## Workflow

### Step 1: Extract Topics
Parse the user's query to identify the specific expertise areas they're looking for.

Common patterns to recognize:
- "who knows about X" → extract X
- "anyone working on Y" → extract Y
- "expert in Z" → extract Z
- "looking for someone in [field]" → extract field
- "recommend someone for [topic]" → extract topic

### Step 2: Vector Search
Use the `vector_search` function to find users with matching expertise.

The search uses cosine similarity on embeddings stored in the `user_expertise` table.

```sql
SELECT u.id, u.name, u.slack_id, e.topic, e.relationship,
       1 - (e.embedding <=> $query_embedding) as similarity
FROM users u
JOIN user_expertise e ON u.id = e.user_id
WHERE 1 - (e.embedding <=> $query_embedding) > 0.7
ORDER BY similarity DESC
LIMIT 5;
```

### Step 3: Format Response
Generate a warm, friendly response suggesting the matched users.

Include for each user:
- Their name (with Slack mention if available)
- Their expertise area that matched
- The relationship type (expert, working on, interested)

## Response Style

- Start with an acknowledgment of what they're looking for
- Use casual Australian language occasionally (mate, no worries, legend, etc.)
- Be encouraging about making connections
- If no users found, offer alternative suggestions or ask for clarification

## Example Responses

### Successful Match
```
G'day! Looking for folks in AI research, eh? Here are some legends who might help:

• **@sam** - Expert in machine learning and neural networks
• **@jane** - Currently working on computer vision projects
• **@bob** - Interested in deep learning applications

Feel free to reach out to them! 🦘
```

### No Matches Found
```
Hmm, I couldn't find anyone specifically matching "quantum computing" in our community yet.

A few things we could try:
• Broaden the search - maybe "physics" or "advanced computing"?
• Post in #introductions asking if anyone's into this space
• Check out our upcoming events - might meet someone there!

Want me to try a different search? 🤔
```

## Error Handling

If the database search fails:
1. Apologize briefly
2. Suggest alternative ways to find help (Slack channels, events)
3. Offer to try again
