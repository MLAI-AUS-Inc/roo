"""Empirical probe of Roo's deterministic (pre-LLM) routing layers.

Imports the REAL content_intent.py and loader.py, and replicates agent.py's
pre-LLM dispatch order 1:1 (fast path -> parse_routing_intent -> _looks_like_*
-> keyword scoring). Anything not caught here falls through to the LLM router.
"""
import re
import sys
from pathlib import Path

REPO = Path("/sessions/hopeful-optimistic-heisenberg/mnt/roo/roo-standalone")
sys.path.insert(0, str(REPO))

from roo.content_intent import parse_routing_intent, normalize_slack_text  # real code
import frontmatter

# ---- load real skills (name, trigger_keywords) from SKILL.md frontmatter ----
skills = []
for d in sorted((REPO / "skills").iterdir()):
    f = d / "SKILL.md"
    if d.is_dir() and f.exists():
        post = frontmatter.load(f)
        skills.append({
            "name": post.metadata.get("name"),
            "keywords": post.metadata.get("trigger_keywords", []) or [],
        })

# ---- 1:1 copies of agent.py private matchers (they are pure functions) ----
def looks_like_content(text):
    pats = (r'\barticle\b', r'\bblog(?:\s+post)?\b', r'\bseo\b', r'\bkeyword\b',
            r'\btopic\b', r'\bwrite\b.*\b(article|blog(?:\s+post)?)\b',
            r'\bresearch\b.*\b(article|topic|keyword)\b', r'\bfor my domain\b')
    return any(re.search(p, text) for p in pats)

def normalize_points_text(text):
    t = str(text or "").lower()
    for typo, rep in {"coworkign": "coworking", "cowokrking": "coworking",
                      "cowokring": "coworking", "co working": "coworking",
                      "co-working": "coworking"}.items():
        t = t.replace(typo, rep)
    return t

def looks_like_points(text):
    text = normalize_points_text(text)
    pats = (r'\bpoints?\b', r'\btop\s*up\b', r'\btopup\b', r'\btop-up\b', r'\bbalance\b',
            r'\bcoworking\b', r'\bbook\s+me\s+in\b', r'\bcheck\b.*<@[a-z0-9]+>.*\bin\b',
            r'\brewards?\b', r'\bclaim\s+task\b', r'\bcreate\s+(?:a\s+)?task\b',
            r'\btask\s+create\b', r'\bworth\s+\d+\s+points?\b')
    return any(re.search(p, text) for p in pats)

def looks_like_luma(text):
    pats = (r'\bluma\b', r'\battendees?\b', r'\bguest\s+lists?\b', r'\bguests?\b.*\bcsv\b',
            r'\bcsv\b.*\bguests?\b', r'\bcsv\b.*\bmlai\s+events?\b', r'\bmlai\s+events?\b.*\bcsv\b',
            r'\bpast\s+csv\s+documents?\b', r'\bregistered\b.*\bevents?\b',
            r'\bregistrations?\b.*\bevents?\b')
    return any(re.search(p, text) for p in pats)

def looks_like_linear_meeting(text, has_file=False):
    has_linear = bool(re.search(r'\blinear\b', text))
    has_src = bool(re.search(r'\b(meeting|transcript|summary|notes?|action\s+items?|to-?dos?|file|pdf|docx?|document|image|screenshot)\b', text)) or has_file
    has_create = bool(re.search(r'\b(extract|sync|turn|send|create|add|tickets?|issues?|tasks?)\b', text))
    return has_linear and has_src and has_create

def keyword_matches(text, kw):
    kw = kw.lower().strip()
    if not kw:
        return False
    return re.search(rf'(?<!\w){re.escape(kw)}(?!\w)', text) is not None

def fast_path(text):
    t = text.lower().strip()
    if re.match(r'^(?:points|balance|my points)$', t): return "balance"
    if re.match(r'^(?:points\s+earn|earn\s+points|ways\s+to\s+earn|tasks(?:\s+(?:all|mine|review|open))?|my\s+tasks|review\s+tasks|open\s+tasks|all\s+tasks)$', t): return "list_tasks"
    if re.match(r'^(?:points\s+rewards|rewards)$', t): return "list_rewards"
    if re.match(r'^coworking\s+book\s+today$', t): return "book_coworking"
    if re.match(r'^coworking\s+cancel$', t): return "cancel_coworking"
    return None

def route(text, has_file=False):
    """Replicates RooAgent.handle_mention pre-LLM dispatch order."""
    clean = " ".join(normalize_slack_text(text).split()).strip()
    fp = fast_path(clean)
    if fp:
        return ("FAST-PATH", f"mlai-points::{fp}")
    ri = parse_routing_intent(clean)          # REAL code from content_intent.py
    if ri:
        return ("REGEX-INTENT", f"{ri['skill_name']} {ri.get('params')}")
    t = clean.lower().strip()
    if looks_like_luma(t):            return ("LOOKS-LIKE", "luma-events")
    if looks_like_content(t):         return ("LOOKS-LIKE", "content-factory")
    if looks_like_linear_meeting(t, has_file): return ("LOOKS-LIKE", "linear-meeting-actions")
    if looks_like_points(t):          return ("LOOKS-LIKE", "mlai-points")
    # keyword scoring (verbatim formula from agent.py)
    scores = {}
    for s in skills:
        matched = [k for k in s["keywords"] if keyword_matches(t, k)]
        if matched:
            scores[s["name"]] = (sum(len(k.split()) * 3 + len(k) for k in matched), matched)
    if scores:
        ranked = sorted(scores.items(), key=lambda i: i[1][0], reverse=True)
        best, (bs, bkw) = ranked[0]
        rs = ranked[1][1][0] if len(ranked) > 1 else -1
        if len(ranked) == 1 or bs >= rs + 4:
            return ("KEYWORDS", f"{best} (matched {bkw}, score {bs} vs {rs})")
        return ("KEYWORD-TIE", f"ambiguous {[(n, sc[0]) for n, sc in ranked[:3]]} -> falls to LLM")
    return ("LLM-FALLBACK", "reaches LLM router")

CASES = [
    # (user message, what the user actually wanted)
    ("do you know anyone in AI research?",                       "connect-users"),
    ("anyone in the community working with medical imaging?",    "connect-users"),
    ("who should I talk to about hackathon sponsorship announcements?", "connect-users"),
    ("can you summarise this article for me?",                   "general/summarise"),
    ("what did you think of the blog post I shared yesterday?",  "general chat"),
    ("please analyse this project proposal",                     "general/analysis"),
    ("can you inspect the CSV I uploaded and tell me the columns?", "general/file Q"),
    ("book me in for coworking tomorrow",                        "mlai-points booking"),
    ("can you book a meeting room for the medhack judges?",      "(no skill exists; general)"),
    ("add a task to linear to fix the login bug",                "linear-meeting-actions"),
    ("create a linear ticket from this thread",                  "linear-meeting-actions"),
    ("how do I earn points?",                                    "mlai-points"),
    ("whats my balance",                                         "mlai-points"),
    ("balance?",                                                 "mlai-points"),
    ("rewrite this announcement in our tone of voice",           "tone-of-voice"),
    ("announce the workshop tomorrow",                           "watt-the-hack or healthhack (channel-dep)"),
    ("how many people registered for the AI safety event?",      "luma-events"),
    ("export the attendee list as a csv",                        "luma-events"),
    ("can you write an article about our medhack winners for the blog?", "content-factory"),
    ("connect me with someone who writes blog content",          "connect-users"),
    ("what tasks are open?",                                     "mlai-points"),
    ("scan the repo for the domain mlai.au",                     "content-factory scan"),
    ("I read a research paper on keyword extraction, thoughts?", "general chat"),
    ("my ecg project needs a teammate, anyone interested in medical AI?", "connect-users"),
    ("what's the topic for this week's meetup?",                 "general/events"),
    ("can you check who's coming to the patient-data workshop?", "luma-events"),
    ("write a summary of this thread",                           "general/summarise"),
    ("research the best time to post on linkedin",               "general research"),
]

print(f"{'LAYER':<14} {'ROUTED TO':<58} MESSAGE")
print("-" * 130)
mis = 0
for msg, wanted in CASES:
    layer, routed = route(msg)
    print(f"{layer:<14} {routed:<58} {msg!r}  [wanted: {wanted}]")
print("\nSkill keyword collision check (keywords claimed by 2+ skills):")
from collections import defaultdict
claim = defaultdict(list)
for s in skills:
    for k in s["keywords"]:
        claim[k.lower()].append(s["name"])
for k, owners in sorted(claim.items()):
    if len(owners) > 1:
        print(f"  {k!r}: {owners}")
