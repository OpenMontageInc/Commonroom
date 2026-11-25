"""Find which members a message @mentions.

Offsets are UTF-16 code units because they come from a browser textarea.
"""
import json
import re


def _free(spans, start, end):
    return all(end <= mark["start"] or start >= mark["end"] for mark in spans)


def tag_mentions(text, roster, labels, selected=None):
    """Return the JSON list of mention spans in ``text``.

    ``roster`` is the active members (each with ``id``, ``name`` and ``aliases``).
    ``labels(member_id)`` returns every name and alias that member has ever had.
    ``selected`` are spans the user picked from the mention menu; they win over
    plain-text matches and survive names shared by several members.
    """
    if "@" not in text:
        return "[]"
    current, aliases, spellings = {}, {}, {}
    for member in roster:
        key = member["name"].casefold()
        current.setdefault(key, set()).add(member["id"])
        spellings[key] = member["name"]
        for alias in member["aliases"]:
            aliases.setdefault(alias.casefold(), set()).add(member["id"])
            spellings.setdefault(alias.casefold(), alias)
    encoded = text.encode("utf-16-le")
    spans = []
    for ref in selected or []:
        if not isinstance(ref, dict):
            continue
        start, end, member_id = (ref.get(k) for k in ("start", "end", "member"))
        if type(start) is not int or type(end) is not int or not (0 <= start < end <= len(encoded) // 2) or not isinstance(member_id, str):
            continue
        literal = encoded[2 * start:2 * end].decode("utf-16-le", errors="replace")
        if literal.startswith("@") and any(literal[1:].casefold() == name.casefold() for name in labels(member_id)):
            if _free(spans, start, end):
                spans.append(dict(start=start, end=end, member=member_id, name=literal[1:]))
    options = sorted(spellings.values(), key=len, reverse=True)
    if options:
        pattern = r"(?<![\w@./])@(" + "|".join(map(re.escape, options)) + r")(?!\w)"
        verbatim = list(re.finditer(r"`{3}[\s\S]*?(?:`{3}|$)|`[^`\n]*`|https?://[^\s<>]+", text, re.IGNORECASE))
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if any(part.start() <= match.start() < part.end() for part in verbatim):
                continue
            key = match[1].casefold()
            owners = current.get(key, aliases.get(key, set()))
            if len(owners) != 1:
                continue
            start = len(text[:match.start()].encode("utf-16-le")) // 2
            end = start + len(match[0].encode("utf-16-le")) // 2
            if _free(spans, start, end):
                spans.append(dict(start=start, end=end, member=next(iter(owners)), name=match[1]))
    return json.dumps(sorted(spans, key=lambda mark: mark["start"]), ensure_ascii=False)
