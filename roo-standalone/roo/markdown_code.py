"""Offset-preserving Markdown code masking for action parsing."""

import re


def mask_markdown_code(text: str) -> str:
    """Exclude fenced/indented blocks and matching-length inline code spans.

    The block/span rules match the backend's signed mention normalizer. An
    unmatched backtick run is conservatively hidden through the line end so
    incomplete examples cannot turn into an award amount.
    """
    chars = list(text)
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", content)
        masked = bool(fence) or bool(re.match(r"^(?: {4}|\t)", content))
        if fence:
            closing = re.match(r"^ {0,3}(`+|~+)[ \t]*$", content)
            if closing and closing[1][0] == fence[0] and len(closing[1]) >= fence[1]:
                fence = None
        elif opening and not (opening[1][0] == "`" and "`" in opening[2]):
            fence = (opening[1][0], len(opening[1]))
            masked = True
        if masked:
            chars[offset:offset + len(content)] = " " * len(content)
        offset += len(line)
    visible = "".join(chars)
    for match in re.finditer(r"(?<![`\\])(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)", visible):
        chars[match.start():match.end()] = " " * len(match[0])
    visible = "".join(chars)
    for match in re.finditer(r"(?<![`\\])`+[^\r\n]*", visible):
        chars[match.start():match.end()] = " " * len(match[0])
    return "".join(chars)
