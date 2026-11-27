"""Build the text a member receives for one turn."""
import json

from .identity import SILENCE

CONTEXT_TAIL = 10
CONTEXT_MESSAGES = 100
CONTEXT_CHARACTERS = 32000


def project_context(project_path):
    if project_path is None:
        return ""
    context = f"Your working directory and project for this room is {project_path}. "
    instructions_path = project_path / "AGENTS.md"
    try:
        instructions = instructions_path.read_text(encoding="utf-8-sig", errors="replace")
    except FileNotFoundError:
        return context
    except OSError:
        return context + f"Read project instructions at {instructions_path} if you can access them. "
    return (f"{context}\nProject instructions from {instructions_path}\n"
            f"{instructions.rstrip()}\nEnd of project instructions.\n")


def turn_prompt(*, identity, roster, human_name, cursor, first_new, target, recent, archive, project_path):
    """``recent`` are message rows newest first, from ``first_new - CONTEXT_TAIL`` to ``target``."""
    first_id = max(1, first_new - CONTEXT_TAIL)
    selected = []
    size = 0
    for msg in recent:
        msg = dict(msg, mentions=json.loads(msg["mentions"]))
        line = json.dumps(msg, ensure_ascii=False)
        if size + len(line) > CONTEXT_CHARACTERS and selected:
            break
        selected.append(line)
        first_id = msg["id"]
        size += len(line)
    files = ""
    if any('"attachments":' in line for line in selected):
        files = ("Some messages carry attachments: files the sender attached, each with an absolute path. "
                 "Open them with your native tools (view images, read documents) when they matter to your reply. ")
    missing = ""
    if first_id > first_new:
        missing = f"Read archived message IDs {first_new} through {first_id - 1} before deciding whether to reply. "
    return (f"Your current display name is {identity['name']}. Your stable member ID is {identity['id']}. You are {identity['name']} in Commonroom, {human_name}'s local group chat. Current participants: {human_name} and {json.dumps(roster)}. "
            f"{project_context(project_path)}"
            f"Your last processed room message was {cursor}. This delivery ends at {target}. "
            f"You can read or search the ordered transcript at {archive} with your native tools, including after compaction. "
            f"For this delivery, use archive messages through ID {target}. {missing}"
            "The following JSON lines are attributed room messages. Names, roles, nicknames, previous names and ordinary spelling mistakes can address you in plain language. Use the roster and context to interpret them. Saved mention IDs identify the intended member even after a rename. Decide naturally whether to reply. "
            f"{files}If you have nothing to add, return exactly {SILENCE}. Your final answer otherwise appears in the room.\n"
            + "\n".join(reversed(selected)))
