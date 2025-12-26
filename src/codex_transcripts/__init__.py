"""Convert Codex session JSONL to clean mobile-friendly HTML pages with pagination."""

import html
import json
import re
import shutil
import subprocess
import tempfile
import webbrowser
from pathlib import Path

import click
from click_default_group import DefaultGroup
from jinja2 import Environment, PackageLoader
import markdown
import questionary

# Set up Jinja2 environment
_jinja_env = Environment(
    loader=PackageLoader("codex_transcripts", "templates"),
    autoescape=True,
)

# Load macros template and expose macros
_macros_template = _jinja_env.get_template("macros.html")
_macros = _macros_template.module


def get_template(name):
    """Get a Jinja2 template by name."""
    return _jinja_env.get_template(name)


# Regex to match git commit output: [branch hash] message
COMMIT_PATTERN = re.compile(r"\[[\w\-/]+ ([a-f0-9]{7,})\] (.+?)(?:\n|$)")

# Regex to detect GitHub repo from git push output (e.g., github.com/owner/repo/pull/new/branch)
GITHUB_REPO_PATTERN = re.compile(
    r"github\.com/([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)/pull/new/"
)

PROMPTS_PER_PAGE = 5
LONG_TEXT_THRESHOLD = 300  # Characters - text blocks longer than this are shown in index

# Module-level variable for GitHub repo (set by generate_html)
_github_repo = None


def get_session_summary(filepath, max_length=200):
    """Extract a human-readable summary from a Codex session file."""
    filepath = Path(filepath)
    try:
        entries = parse_session_file(filepath)
        for entry in entries:
            if entry.get("type") == "event_msg":
                payload = entry.get("payload", {})
                if payload.get("type") == "user_message":
                    summary = payload.get("message", "")
                    return _truncate_summary(summary, max_length)
            if entry.get("type") == "response_item":
                payload = entry.get("payload", {})
                if (
                    payload.get("type") == "message"
                    and payload.get("role") == "user"
                    and payload.get("content")
                ):
                    text = _content_items_to_text(payload.get("content", []))
                    return _truncate_summary(text, max_length)
        return "(no summary)"
    except Exception:
        return "(no summary)"


def _truncate_summary(text, max_length):
    text = (text or "").strip()
    if not text:
        return "(no summary)"
    if len(text) > max_length:
        return text[: max_length - 3] + "..."
    return text


def find_local_sessions(folder, limit=10):
    """Find recent Codex JSONL session files in the given folder."""
    folder = Path(folder)
    if not folder.exists():
        return []

    results = []
    for f in folder.rglob("*.jsonl"):
        if not f.name.startswith("rollout-"):
            continue
        summary = get_session_summary(f)
        results.append((f, summary))

    # Sort by modification time, most recent first
    results.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    return results[:limit]


def parse_session_file(filepath):
    """Parse a Codex session file and return a list of rollout line dicts."""
    filepath = Path(filepath)
    if filepath.suffix == ".jsonl":
        return _parse_jsonl_file(filepath)
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "entries" in data and isinstance(data["entries"], list):
            return data["entries"]
        if "loglines" in data and isinstance(data["loglines"], list):
            return data["loglines"]
    return []


def _parse_jsonl_file(filepath):
    entries = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _content_items_to_text(items):
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("output_text", "input_text", "text"):
            text = item.get("text", "")
            if text:
                parts.append(text)
        elif item.get("type") == "input_image":
            url = item.get("image_url")
            if url:
                parts.append(f"![image]({url})")
    return "\n".join(parts)


def _content_items_to_blocks(items):
    blocks = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in ("output_text", "input_text", "text"):
            text = item.get("text", "")
            if text:
                blocks.append({"type": "text", "text": text})
        elif item_type == "input_image":
            url = item.get("image_url")
            if url:
                blocks.append({"type": "text", "text": f"![image]({url})"})
    return blocks


def _tool_use_block(name, tool_input, tool_id=None):
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}
    return {
        "type": "tool_use",
        "name": name,
        "input": tool_input or {},
        "id": tool_id or "",
    }


def _tool_result_block(content, is_error=False):
    return {
        "type": "tool_result",
        "content": content,
        "is_error": is_error,
    }


def _normalize_event_message(payload, timestamp):
    event_type = payload.get("type")
    if event_type == "user_message":
        message = payload.get("message", "")
        blocks = [{"type": "text", "text": message}] if message else []
        images = payload.get("images") or []
        for url in images:
            blocks.append({"type": "text", "text": f"![image]({url})"})
        return {
            "role": "user",
            "timestamp": timestamp,
            "content": blocks,
        }
    if event_type == "agent_message":
        message = payload.get("message", "")
        return {
            "role": "assistant",
            "timestamp": timestamp,
            "content": [{"type": "text", "text": message}] if message else [],
        }
    if event_type in ("agent_reasoning", "agent_reasoning_raw_content"):
        text = payload.get("text", "")
        return {
            "role": "assistant",
            "timestamp": timestamp,
            "content": [{"type": "thinking", "thinking": text}] if text else [],
        }
    return None


def _parse_json_maybe(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    if (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    ):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def _normalize_response_item(payload, timestamp, include_messages):
    item_type = payload.get("type")
    if item_type == "message":
        if not include_messages:
            return []
        role = payload.get("role", "assistant")
        blocks = _content_items_to_blocks(payload.get("content", []))
        return [
            {
                "role": role,
                "timestamp": timestamp,
                "content": blocks,
            }
        ]
    if item_type == "reasoning":
        parts = []
        for summary in payload.get("summary", []) or []:
            if isinstance(summary, dict) and summary.get("type") == "summary_text":
                text = summary.get("text", "")
                if text:
                    parts.append(text)
        for content in payload.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            ctype = content.get("type")
            if ctype in ("reasoning_text", "text"):
                text = content.get("text", "")
                if text:
                    parts.append(text)
        if not parts:
            return []
        return [
            {
                "role": "assistant",
                "timestamp": timestamp,
                "content": [{"type": "thinking", "thinking": "\n\n".join(parts)}],
            }
        ]
    if item_type == "local_shell_call":
        action = payload.get("action", {})
        command = " ".join(action.get("command", []) or [])
        tool_input = {
            "command": command,
            "description": "Local shell",
            "status": payload.get("status"),
            "working_directory": action.get("working_directory"),
            "timeout_ms": action.get("timeout_ms"),
            "env": action.get("env"),
        }
        return [
            {
                "role": "assistant",
                "timestamp": timestamp,
                "content": [_tool_use_block("Bash", tool_input, payload.get("call_id"))],
            }
        ]
    if item_type == "function_call":
        tool_input = _parse_json_maybe(payload.get("arguments", ""))
        return [
            {
                "role": "assistant",
                "timestamp": timestamp,
                "content": [
                    _tool_use_block(payload.get("name", "Function"), tool_input, payload.get("call_id"))
                ],
            }
        ]
    if item_type == "custom_tool_call":
        tool_input = _parse_json_maybe(payload.get("input", ""))
        return [
            {
                "role": "assistant",
                "timestamp": timestamp,
                "content": [
                    _tool_use_block(payload.get("name", "Tool"), tool_input, payload.get("call_id"))
                ],
            }
        ]
    if item_type == "function_call_output":
        return [
            {
                "role": "assistant",
                "timestamp": timestamp,
                "content": [_tool_result_block(payload.get("output"))],
            }
        ]
    if item_type == "custom_tool_call_output":
        return [
            {
                "role": "assistant",
                "timestamp": timestamp,
                "content": [_tool_result_block(payload.get("output"))],
            }
        ]
    if item_type == "web_search_call":
        action = payload.get("action", {})
        return [
            {
                "role": "assistant",
                "timestamp": timestamp,
                "content": [_tool_use_block("web_search", action, payload.get("call_id"))],
            }
        ]
    if item_type == "compaction":
        return [
            {
                "role": "assistant",
                "timestamp": timestamp,
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Context compacted.",
                    }
                ],
            }
        ]
    if item_type == "ghost_snapshot":
        return [
            {
                "role": "assistant",
                "timestamp": timestamp,
                "content": [_tool_result_block(payload)],
            }
        ]
    return []


def normalize_rollout_entries(entries):
    """Convert raw rollout entries into message dicts."""
    has_event_messages = any(
        entry.get("type") == "event_msg"
        and entry.get("payload", {}).get("type")
        in ("user_message", "agent_message", "agent_reasoning", "agent_reasoning_raw_content")
        for entry in entries
    )

    messages = []
    for entry in entries:
        item_type = entry.get("type")
        payload = entry.get("payload", {})
        timestamp = entry.get("timestamp", "")

        if item_type == "event_msg":
            msg = _normalize_event_message(payload, timestamp)
            if msg and msg.get("content"):
                messages.append(msg)
            continue

        if item_type == "compacted":
            message = payload.get("message", "")
            if message:
                messages.append(
                    {
                        "role": "assistant",
                        "timestamp": timestamp,
                        "content": [{"type": "thinking", "thinking": message}],
                        "is_preface": True,
                    }
                )
            continue

        if item_type == "response_item":
            messages.extend(_normalize_response_item(payload, timestamp, not has_event_messages))
            continue

    return messages


def detect_github_repo(messages):
    """Detect GitHub repo from git push output in tool results."""
    for message in messages:
        for block in message.get("content", []):
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                if isinstance(content, str):
                    match = GITHUB_REPO_PATTERN.search(content)
                    if match:
                        return match.group(1)
    return None


def format_json(obj):
    try:
        if isinstance(obj, str):
            obj = json.loads(obj)
        formatted = json.dumps(obj, indent=2, ensure_ascii=False)
        return f'<pre class="json">{html.escape(formatted)}</pre>'
    except (json.JSONDecodeError, TypeError):
        return f"<pre>{html.escape(str(obj))}</pre>"


def render_markdown_text(text):
    if not text:
        return ""
    return markdown.markdown(text, extensions=["fenced_code", "tables"])


def is_json_like(text):
    if not text or not isinstance(text, str):
        return False
    text = text.strip()
    return (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    )


def render_todo_write(tool_input, tool_id):
    todos = tool_input.get("todos", [])
    if not todos:
        return ""
    return _macros.todo_list(todos, tool_id)


def render_write_tool(tool_input, tool_id):
    """Render Write tool calls with file path header and content preview."""
    file_path = tool_input.get("file_path", "Unknown file")
    content = tool_input.get("content", "")
    return _macros.write_tool(file_path, content, tool_id)


def render_edit_tool(tool_input, tool_id):
    """Render Edit tool calls with diff-like old/new display."""
    file_path = tool_input.get("file_path", "Unknown file")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")
    replace_all = tool_input.get("replace_all", False)
    return _macros.edit_tool(file_path, old_string, new_string, replace_all, tool_id)


def render_bash_tool(tool_input, tool_id):
    """Render Bash tool calls with command as plain text."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")
    return _macros.bash_tool(command, description, tool_id)


def render_content_block(block):
    if not isinstance(block, dict):
        return f"<p>{html.escape(str(block))}</p>"
    block_type = block.get("type", "")
    if block_type == "thinking":
        content_html = render_markdown_text(block.get("thinking", ""))
        return _macros.thinking(content_html)
    if block_type == "text":
        content_html = render_markdown_text(block.get("text", ""))
        return _macros.assistant_text(content_html)
    if block_type == "tool_use":
        tool_name = block.get("name", "Unknown tool")
        tool_input = block.get("input", {})
        tool_id = block.get("id", "")
        if tool_name == "TodoWrite":
            return render_todo_write(tool_input, tool_id)
        if tool_name == "Write":
            return render_write_tool(tool_input, tool_id)
        if tool_name == "Edit":
            return render_edit_tool(tool_input, tool_id)
        if tool_name == "Bash":
            return render_bash_tool(tool_input, tool_id)
        description = tool_input.get("description", "")
        display_input = {k: v for k, v in tool_input.items() if k != "description"}
        input_json = json.dumps(display_input, indent=2, ensure_ascii=False)
        return _macros.tool_use(tool_name, description, input_json, tool_id)
    if block_type == "tool_result":
        content = block.get("content", "")
        is_error = block.get("is_error", False)

        # Check for git commits and render with styled cards
        if isinstance(content, str):
            commits_found = list(COMMIT_PATTERN.finditer(content))
            if commits_found:
                parts = []
                last_end = 0
                for match in commits_found:
                    before = content[last_end: match.start()].strip()
                    if before:
                        parts.append(f"<pre>{html.escape(before)}</pre>")

                    commit_hash = match.group(1)
                    commit_msg = match.group(2)
                    parts.append(_macros.commit_card(commit_hash, commit_msg, _github_repo))
                    last_end = match.end()

                after = content[last_end:].strip()
                if after:
                    parts.append(f"<pre>{html.escape(after)}</pre>")

                content_html = "".join(parts)
            else:
                content_html = f"<pre>{html.escape(content)}</pre>"
        elif isinstance(content, list) or is_json_like(content):
            content_html = format_json(content)
        else:
            content_html = format_json(content)
        return _macros.tool_result(content_html, is_error)

    return format_json(block)


def is_tool_result_message(message_data):
    """Check if a message contains only tool_result blocks."""
    content = message_data.get("content", [])
    if not isinstance(content, list):
        return False
    if not content:
        return False
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def _render_block_for_role(block, role):
    if role == "user" and isinstance(block, dict) and block.get("type") == "text":
        content_html = render_markdown_text(block.get("text", ""))
        return _macros.user_content(content_html)
    return render_content_block(block)


def render_message(message):
    role = message.get("role", "assistant")
    content_html = "".join(
        _render_block_for_role(block, role) for block in message.get("content", [])
    )
    if not content_html.strip():
        return ""
    timestamp = message.get("timestamp", "")
    msg_id = make_msg_id(timestamp) if timestamp else ""

    if role == "user":
        role_class, role_label = "user", "User"
    else:
        role_class, role_label = "assistant", "Assistant"

    if is_tool_result_message(message):
        role_class, role_label = "tool-reply", "Tool reply"

    return _macros.message(role_class, role_label, msg_id, timestamp, content_html)


def make_msg_id(timestamp):
    return f"msg-{timestamp.replace(':', '-').replace('.', '-')}"


def analyze_conversation(messages):
    """Analyze messages in a conversation to extract stats and long texts."""
    tool_counts = {}
    long_texts = []
    commits = []

    for message in messages:
        for block in message.get("content", []):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")

            if block_type == "tool_use":
                tool_name = block.get("name", "Unknown")
                tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
            elif block_type == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    for match in COMMIT_PATTERN.finditer(result_content):
                        commits.append((match.group(1), match.group(2), message.get("timestamp", "")))
            elif block_type == "text":
                text = block.get("text", "")
                if len(text) >= LONG_TEXT_THRESHOLD:
                    long_texts.append(text)

    return {
        "tool_counts": tool_counts,
        "long_texts": long_texts,
        "commits": commits,
    }


def format_tool_stats(tool_counts):
    """Format tool counts into a concise summary string."""
    if not tool_counts:
        return ""

    abbrev = {
        "Bash": "bash",
        "Read": "read",
        "Write": "write",
        "Edit": "edit",
        "Glob": "glob",
        "Grep": "grep",
        "Task": "task",
        "TodoWrite": "todo",
        "WebFetch": "fetch",
        "WebSearch": "search",
    }

    parts = []
    for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        short_name = abbrev.get(name, name.lower())
        parts.append(f"{count} {short_name}")

    return " · ".join(parts)


CSS = """
:root { --bg-color: #f5f5f5; --card-bg: #ffffff; --user-bg: #e3f2fd; --user-border: #1976d2; --assistant-bg: #f5f5f5; --assistant-border: #9e9e9e; --thinking-bg: #fff8e1; --thinking-border: #ffc107; --thinking-text: #666; --tool-bg: #f3e5f5; --tool-border: #9c27b0; --tool-result-bg: #e8f5e9; --tool-error-bg: #ffebee; --text-color: #212121; --text-muted: #757575; --code-bg: #263238; --code-text: #aed581; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg-color); color: var(--text-color); margin: 0; padding: 16px; line-height: 1.6; }
.container { max-width: 800px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin-bottom: 24px; padding-bottom: 8px; border-bottom: 2px solid var(--user-border); }
.message { margin-bottom: 16px; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.message.user { background: var(--user-bg); border-left: 4px solid var(--user-border); }
.message.assistant { background: var(--card-bg); border-left: 4px solid var(--assistant-border); }
.message.tool-reply { background: #fff8e1; border-left: 4px solid #ff9800; }
.tool-reply .role-label { color: #e65100; }
.tool-reply .tool-result { background: transparent; padding: 0; margin: 0; }
.tool-reply .tool-result .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, #fff8e1); }
.message-header { display: flex; justify-content: space-between; align-items: center; padding: 8px 16px; background: rgba(0,0,0,0.03); font-size: 0.85rem; }
.role-label { font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
.user .role-label { color: var(--user-border); }
time { color: var(--text-muted); font-size: 0.8rem; }
.timestamp-link { color: inherit; text-decoration: none; }
.timestamp-link:hover { text-decoration: underline; }
.message:target { animation: highlight 2s ease-out; }
@keyframes highlight { 0% { background-color: rgba(25, 118, 210, 0.2); } 100% { background-color: transparent; } }
.message-content { padding: 16px; }
.message-content p { margin: 0 0 12px 0; }
.message-content p:last-child { margin-bottom: 0; }
.thinking { background: var(--thinking-bg); border: 1px solid var(--thinking-border); border-radius: 8px; padding: 12px; margin: 12px 0; font-size: 0.9rem; color: var(--thinking-text); }
.thinking-label { font-size: 0.75rem; font-weight: 600; text-transform: uppercase; color: #f57c00; margin-bottom: 8px; }
.thinking p { margin: 8px 0; }
.assistant-text { margin: 8px 0; }
.tool-use { background: var(--tool-bg); border: 1px solid var(--tool-border); border-radius: 8px; padding: 12px; margin: 12px 0; }
.tool-header { font-weight: 600; color: var(--tool-border); margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
.tool-icon { font-size: 1.1rem; }
.tool-description { font-size: 0.9rem; color: var(--text-muted); margin-bottom: 8px; font-style: italic; }
.tool-result { background: var(--tool-result-bg); border-radius: 8px; padding: 12px; margin: 12px 0; }
.tool-result.tool-error { background: var(--tool-error-bg); }
.file-tool { border-radius: 8px; padding: 12px; margin: 12px 0; }
.write-tool { background: linear-gradient(135deg, #e3f2fd 0%, #e8f5e9 100%); border: 1px solid #4caf50; }
.edit-tool { background: linear-gradient(135deg, #fff3e0 0%, #fce4ec 100%); border: 1px solid #ff9800; }
.file-tool-header { font-weight: 600; margin-bottom: 4px; display: flex; align-items: center; gap: 8px; font-size: 0.95rem; }
.write-header { color: #2e7d32; }
.edit-header { color: #e65100; }
.file-tool-icon { font-size: 1rem; }
.file-tool-path { font-family: monospace; background: rgba(0,0,0,0.08); padding: 2px 8px; border-radius: 4px; }
.file-tool-fullpath { font-family: monospace; font-size: 0.8rem; color: var(--text-muted); margin-bottom: 8px; word-break: break-all; }
.file-content { margin: 0; }
.edit-section { display: flex; margin: 4px 0; border-radius: 4px; overflow: hidden; }
.edit-label { padding: 8px 12px; font-weight: bold; font-family: monospace; display: flex; align-items: flex-start; }
.edit-old { background: #fce4ec; }
.edit-old .edit-label { color: #b71c1c; background: #f8bbd9; }
.edit-old .edit-content { color: #880e4f; }
.edit-new { background: #e8f5e9; }
.edit-new .edit-label { color: #1b5e20; background: #a5d6a7; }
.edit-new .edit-content { color: #1b5e20; }
.edit-content { margin: 0; flex: 1; background: transparent; font-size: 0.85rem; }
.edit-replace-all { font-size: 0.75rem; font-weight: normal; color: var(--text-muted); }
.write-tool .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, #e6f4ea); }
.edit-tool .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, #fff0e5); }
.todo-list { background: linear-gradient(135deg, #e8f5e9 0%, #f1f8e9 100%); border: 1px solid #81c784; border-radius: 8px; padding: 12px; margin: 12px 0; }
.todo-header { font-weight: 600; color: #2e7d32; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; font-size: 0.95rem; }
.todo-items { list-style: none; margin: 0; padding: 0; }
.todo-item { display: flex; align-items: flex-start; gap: 10px; padding: 6px 0; border-bottom: 1px solid rgba(0,0,0,0.06); font-size: 0.9rem; }
.todo-item:last-child { border-bottom: none; }
.todo-icon { flex-shrink: 0; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; font-weight: bold; border-radius: 50%; }
.todo-completed .todo-icon { color: #2e7d32; background: rgba(46, 125, 50, 0.15); }
.todo-completed .todo-content { color: #558b2f; text-decoration: line-through; }
.todo-in-progress .todo-icon { color: #f57c00; background: rgba(245, 124, 0, 0.15); }
.todo-in-progress .todo-content { color: #e65100; font-weight: 500; }
.todo-pending .todo-icon { color: #757575; background: rgba(0,0,0,0.05); }
.todo-pending .todo-content { color: #616161; }
pre { background: var(--code-bg); color: var(--code-text); padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 0.85rem; line-height: 1.5; margin: 8px 0; white-space: pre-wrap; word-wrap: break-word; }
pre.json { color: #e0e0e0; }
code { background: rgba(0,0,0,0.08); padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
pre code { background: none; padding: 0; }
.user-content { margin: 0; }
.truncatable { position: relative; }
.truncatable.truncated .truncatable-content { max-height: 200px; overflow: hidden; }
.truncatable.truncated::after { content: ''; position: absolute; bottom: 32px; left: 0; right: 0; height: 60px; background: linear-gradient(to bottom, transparent, var(--card-bg)); pointer-events: none; }
.message.user .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--user-bg)); }
.message.tool-reply .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, #fff8e1); }
.tool-use .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--tool-bg)); }
.tool-result .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--tool-result-bg)); }
.expand-btn { display: none; width: 100%; padding: 8px 16px; margin-top: 4px; background: rgba(0,0,0,0.05); border: 1px solid rgba(0,0,0,0.1); border-radius: 6px; cursor: pointer; font-size: 0.85rem; color: var(--text-muted); }
.expand-btn:hover { background: rgba(0,0,0,0.1); }
.truncatable.truncated .expand-btn, .truncatable.expanded .expand-btn { display: block; }
.pagination { display: flex; justify-content: center; gap: 8px; margin: 24px 0; flex-wrap: wrap; }
.pagination a, .pagination span { padding: 5px 10px; border-radius: 6px; text-decoration: none; font-size: 0.85rem; }
.pagination a { background: var(--card-bg); color: var(--user-border); border: 1px solid var(--user-border); }
.pagination a:hover { background: var(--user-bg); }
.pagination .current { background: var(--user-border); color: white; }
.pagination .disabled { color: var(--text-muted); border: 1px solid #ddd; }
.pagination .index-link { background: var(--user-border); color: white; }
details.continuation { margin-bottom: 16px; }
details.continuation summary { cursor: pointer; padding: 12px 16px; background: var(--user-bg); border-left: 4px solid var(--user-border); border-radius: 12px; font-weight: 500; color: var(--text-muted); }
details.continuation summary:hover { background: rgba(25, 118, 210, 0.15); }
details.continuation[open] summary { border-radius: 12px 12px 0 0; margin-bottom: 0; }
.index-item { margin-bottom: 16px; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); background: var(--user-bg); border-left: 4px solid var(--user-border); }
.index-item a { display: block; text-decoration: none; color: inherit; }
.index-item a:hover { background: rgba(25, 118, 210, 0.1); }
.index-item-header { display: flex; justify-content: space-between; align-items: center; padding: 8px 16px; background: rgba(0,0,0,0.03); font-size: 0.85rem; }
.index-item-number { font-weight: 600; color: var(--user-border); }
.index-item-content { padding: 16px; }
.index-item-stats { padding: 8px 16px 12px 32px; font-size: 0.85rem; color: var(--text-muted); border-top: 1px solid rgba(0,0,0,0.06); }
.index-item-commit { margin-top: 6px; padding: 4px 8px; background: #fff3e0; border-radius: 4px; font-size: 0.85rem; color: #e65100; }
.index-item-commit code { background: rgba(0,0,0,0.08); padding: 1px 4px; border-radius: 3px; font-size: 0.8rem; margin-right: 6px; }
.commit-card { margin: 8px 0; padding: 10px 14px; background: #fff3e0; border-left: 4px solid #ff9800; border-radius: 6px; }
.commit-card a { text-decoration: none; color: #5d4037; display: block; }
.commit-card a:hover { color: #e65100; }
.commit-card-hash { font-family: monospace; color: #e65100; font-weight: 600; margin-right: 8px; }
.index-commit { margin-bottom: 12px; padding: 10px 16px; background: #fff3e0; border-left: 4px solid #ff9800; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
.index-commit a { display: block; text-decoration: none; color: inherit; }
.index-commit a:hover { background: rgba(255, 152, 0, 0.1); margin: -10px -16px; padding: 10px 16px; border-radius: 8px; }
.index-commit-header { display: flex; justify-content: space-between; align-items: center; font-size: 0.85rem; margin-bottom: 4px; }
.index-commit-hash { font-family: monospace; color: #e65100; font-weight: 600; }
.index-commit-msg { color: #5d4037; }
.index-item-long-text { margin-top: 8px; padding: 12px; background: var(--card-bg); border-radius: 8px; border-left: 3px solid var(--assistant-border); }
.index-item-long-text .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--card-bg)); }
.index-item-long-text-content { color: var(--text-color); }
@media (max-width: 600px) { body { padding: 8px; } .message, .index-item { border-radius: 8px; } .message-content, .index-item-content { padding: 12px; } pre { font-size: 0.8rem; padding: 8px; } }
"""

JS = """
document.querySelectorAll('time[data-timestamp]').forEach(function(el) {
    const timestamp = el.getAttribute('data-timestamp');
    const date = new Date(timestamp);
    const now = new Date();
    const isToday = date.toDateString() === now.toDateString();
    const timeStr = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    if (isToday) { el.textContent = timeStr; }
    else { el.textContent = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + timeStr; }
});
document.querySelectorAll('pre.json').forEach(function(el) {
    let text = el.textContent;
    text = text.replace(/"([^"]+)":/g, '<span style="color: #ce93d8">"$1"</span>:');
    text = text.replace(/: "([^"]*)"/g, ': <span style="color: #81d4fa">"$1"</span>');
    text = text.replace(/: (\d+)/g, ': <span style="color: #ffcc80">$1</span>');
    text = text.replace(/: (true|false|null)/g, ': <span style="color: #f48fb1">$1</span>');
    el.innerHTML = text;
});
document.querySelectorAll('.truncatable').forEach(function(wrapper) {
    const content = wrapper.querySelector('.truncatable-content');
    const btn = wrapper.querySelector('.expand-btn');
    if (content.scrollHeight > 250) {
        wrapper.classList.add('truncated');
        btn.addEventListener('click', function() {
            if (wrapper.classList.contains('truncated')) { wrapper.classList.remove('truncated'); wrapper.classList.add('expanded'); btn.textContent = 'Show less'; }
            else { wrapper.classList.remove('expanded'); wrapper.classList.add('truncated'); btn.textContent = 'Show more'; }
        });
    }
});
"""

# JavaScript to fix relative URLs when served via gistpreview.github.io
GIST_PREVIEW_JS = r"""
(function() {
    if (window.location.hostname !== 'gistpreview.github.io') return;
    // URL format: https://gistpreview.github.io/?GIST_ID/filename.html
    var match = window.location.search.match(/^\?([^/]+)/);
    if (!match) return;
    var gistId = match[1];
    document.querySelectorAll('a[href]').forEach(function(link) {
        var href = link.getAttribute('href');
        // Skip external links and anchors
        if (href.startsWith('http') || href.startsWith('#') || href.startsWith('//')) return;
        // Handle anchor in relative URL (e.g., page-001.html#msg-123)
        var parts = href.split('#');
        var filename = parts[0];
        var anchor = parts.length > 1 ? '#' + parts[1] : '';
        link.setAttribute('href', '?' + gistId + '/' + filename + anchor);
    });
})();
"""


def inject_gist_preview_js(output_dir):
    """Inject gist preview JavaScript into all HTML files in the output directory."""
    output_dir = Path(output_dir)
    for html_file in output_dir.glob("*.html"):
        content = html_file.read_text(encoding="utf-8")
        if "</body>" in content:
            content = content.replace(
                "</body>", f"<script>{GIST_PREVIEW_JS}</script>\n</body>"
            )
            html_file.write_text(content, encoding="utf-8")


def create_gist(output_dir, public=False):
    """Create a GitHub gist from the HTML files in output_dir."""
    output_dir = Path(output_dir)
    html_files = list(output_dir.glob("*.html"))
    if not html_files:
        raise click.ClickException("No HTML files found to upload to gist.")

    cmd = ["gh", "gist", "create"]
    cmd.extend(str(f) for f in sorted(html_files))
    if public:
        cmd.append("--public")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        gist_url = result.stdout.strip()
        gist_id = gist_url.rstrip("/").split("/")[-1]
        return gist_id, gist_url
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() if e.stderr else str(e)
        raise click.ClickException(f"Failed to create gist: {error_msg}")
    except FileNotFoundError:
        raise click.ClickException(
            "gh CLI not found. Install it from https://cli.github.com/ and run 'gh auth login'."
        )


def generate_pagination_html(current_page, total_pages):
    return _macros.pagination(current_page, total_pages)


def generate_index_pagination_html(total_pages):
    return _macros.index_pagination(total_pages)


def _group_conversations(messages):
    conversations = []
    current_conv = None
    preface = []

    for msg in messages:
        if msg.get("role") == "user":
            if current_conv:
                conversations.append(current_conv)
            current_conv = {
                "user_text": _extract_user_text(msg),
                "timestamp": msg.get("timestamp", ""),
                "messages": [],
                "has_preface": False,
                "is_continuation": False,
            }
            if preface:
                current_conv["messages"].extend(preface)
                current_conv["has_preface"] = True
                preface = []
            current_conv["messages"].append(msg)
        else:
            if current_conv:
                current_conv["messages"].append(msg)
            else:
                preface.append(msg)

    if current_conv:
        conversations.append(current_conv)
    elif preface:
        conversations.append(
            {
                "user_text": "(no user prompt)",
                "timestamp": preface[0].get("timestamp", ""),
                "messages": preface,
                "has_preface": False,
                "is_continuation": True,
            }
        )

    return conversations


def _extract_user_text(message):
    for block in message.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            if text:
                return text
    return "(no text)"


def generate_html(json_path, output_dir, github_repo=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)

    entries = parse_session_file(json_path)
    messages = normalize_rollout_entries(entries)

    if github_repo is None:
        github_repo = detect_github_repo(messages)
        if github_repo:
            print(f"Auto-detected GitHub repo: {github_repo}")
        else:
            print(
                "Warning: Could not auto-detect GitHub repo. Commit links will be disabled."
            )

    global _github_repo
    _github_repo = github_repo

    conversations = _group_conversations(messages)

    total_convs = len(conversations)
    total_pages = (total_convs + PROMPTS_PER_PAGE - 1) // PROMPTS_PER_PAGE

    for page_num in range(1, total_pages + 1):
        start_idx = (page_num - 1) * PROMPTS_PER_PAGE
        end_idx = min(start_idx + PROMPTS_PER_PAGE, total_convs)
        page_convs = conversations[start_idx:end_idx]
        messages_html = []
        for conv in page_convs:
            is_first = True
            for message in conv["messages"]:
                msg_html = render_message(message)
                if msg_html:
                    if is_first and conv.get("has_preface"):
                        msg_html = (
                            "<details class=\"continuation\"><summary>Session preface</summary>"
                            f"{msg_html}</details>"
                        )
                    messages_html.append(msg_html)
                is_first = False
        pagination_html = generate_pagination_html(page_num, total_pages)
        page_template = get_template("page.html")
        page_content = page_template.render(
            css=CSS,
            js=JS,
            page_num=page_num,
            total_pages=total_pages,
            pagination_html=pagination_html,
            messages_html="".join(messages_html),
        )
        (output_dir / f"page-{page_num:03d}.html").write_text(
            page_content, encoding="utf-8"
        )
        print(f"Generated page-{page_num:03d}.html")

    total_tool_counts = {}
    total_messages = 0
    all_commits = []
    for i, conv in enumerate(conversations):
        total_messages += len(conv["messages"])
        stats = analyze_conversation(conv["messages"])
        for tool, count in stats["tool_counts"].items():
            total_tool_counts[tool] = total_tool_counts.get(tool, 0) + count
        page_num = (i // PROMPTS_PER_PAGE) + 1
        for commit_hash, commit_msg, commit_ts in stats["commits"]:
            all_commits.append((commit_ts, commit_hash, commit_msg, page_num, i))
    total_tool_calls = sum(total_tool_counts.values())
    total_commits = len(all_commits)

    timeline_items = []

    prompt_num = 0
    for i, conv in enumerate(conversations):
        if conv.get("is_continuation"):
            continue
        prompt_num += 1
        page_num = (i // PROMPTS_PER_PAGE) + 1
        msg_id = make_msg_id(conv["timestamp"])
        link = f"page-{page_num:03d}.html#{msg_id}"
        rendered_content = render_markdown_text(conv["user_text"])

        all_messages = list(conv["messages"])
        stats = analyze_conversation(all_messages)
        tool_stats_str = format_tool_stats(stats["tool_counts"])

        long_texts_html = ""
        for lt in stats["long_texts"]:
            rendered_lt = render_markdown_text(lt)
            long_texts_html += _macros.index_long_text(rendered_lt)

        stats_html = _macros.index_stats(tool_stats_str, long_texts_html)

        item_html = _macros.index_item(
            prompt_num, link, conv["timestamp"], rendered_content, stats_html
        )
        timeline_items.append((conv["timestamp"], "prompt", item_html))

    for commit_ts, commit_hash, commit_msg, page_num, conv_idx in all_commits:
        item_html = _macros.index_commit(commit_hash, commit_msg, commit_ts, _github_repo)
        timeline_items.append((commit_ts, "commit", item_html))

    timeline_items.sort(key=lambda x: x[0])
    index_items = [item[2] for item in timeline_items]

    index_pagination = generate_index_pagination_html(total_pages)
    index_template = get_template("index.html")
    index_content = index_template.render(
        css=CSS,
        js=JS,
        pagination_html=index_pagination,
        prompt_num=prompt_num,
        total_messages=total_messages,
        total_tool_calls=total_tool_calls,
        total_commits=total_commits,
        total_pages=total_pages,
        index_items_html="".join(index_items),
    )
    index_path = output_dir / "index.html"
    index_path.write_text(index_content, encoding="utf-8")
    print(
        f"Generated {index_path.resolve()} ({total_convs} prompts, {total_pages} pages)"
    )


@click.group(cls=DefaultGroup, default="local", default_if_no_args=True)
@click.version_option(None, "-v", "--version", package_name="codex-transcripts")
def cli():
    """Convert Codex session JSONL to mobile-friendly HTML pages."""
    pass


@cli.command("local")
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Output directory. If not specified, writes to temp dir and opens in browser.",
)
@click.option(
    "-a",
    "--output-auto",
    is_flag=True,
    help="Auto-name output subdirectory based on session filename (uses -o as parent, or current dir).",
)
@click.option(
    "--repo",
    help="GitHub repo (owner/name) for commit links. Auto-detected from git output if not specified.",
)
@click.option(
    "--gist",
    is_flag=True,
    help="Upload to GitHub Gist and output a gistpreview.github.io URL.",
)
@click.option(
    "--json",
    "include_json",
    is_flag=True,
    help="Include the original JSONL session file in the output directory.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the generated index.html in your default browser (default if no -o specified).",
)
@click.option(
    "--limit",
    default=10,
    help="Maximum number of sessions to show (default: 10)",
)
def local_cmd(output, output_auto, repo, gist, include_json, open_browser, limit):
    """Select and convert a local Codex session to HTML."""
    from datetime import datetime

    sessions_folder = Path.home() / ".codex" / "sessions"

    if not sessions_folder.exists():
        click.echo(f"Sessions folder not found: {sessions_folder}")
        click.echo("No local Codex sessions available.")
        return

    click.echo("Loading local sessions...")
    results = find_local_sessions(sessions_folder, limit=limit)

    if not results:
        click.echo("No local sessions found.")
        return

    choices = []
    for filepath, summary in results:
        stat = filepath.stat()
        mod_time = datetime.fromtimestamp(stat.st_mtime)
        size_kb = stat.st_size / 1024
        date_str = mod_time.strftime("%Y-%m-%d %H:%M")
        if len(summary) > 50:
            summary = summary[:47] + "..."
        display = f"{date_str}  {size_kb:5.0f} KB  {summary}"
        choices.append(questionary.Choice(title=display, value=filepath))

    selected = questionary.select(
        "Select a session to convert:",
        choices=choices,
    ).ask()

    if selected is None:
        click.echo("No session selected.")
        return

    session_file = selected

    auto_open = output is None and not gist and not output_auto
    if output_auto:
        parent_dir = Path(output) if output else Path(".")
        output = parent_dir / session_file.stem
    elif output is None:
        output = Path(tempfile.gettempdir()) / f"codex-session-{session_file.stem}"

    output = Path(output)
    generate_html(session_file, output, github_repo=repo)

    click.echo(f"Output: {output.resolve()}")

    if include_json:
        output.mkdir(exist_ok=True)
        json_dest = output / session_file.name
        shutil.copy(session_file, json_dest)
        json_size_kb = json_dest.stat().st_size / 1024
        click.echo(f"JSONL: {json_dest} ({json_size_kb:.1f} KB)")

    if gist:
        inject_gist_preview_js(output)
        click.echo("Creating GitHub gist...")
        gist_id, gist_url = create_gist(output)
        preview_url = f"https://gistpreview.github.io/?{gist_id}/index.html"
        click.echo(f"Gist: {gist_url}")
        click.echo(f"Preview: {preview_url}")

    if open_browser or auto_open:
        index_url = (output / "index.html").resolve().as_uri()
        webbrowser.open(index_url)


@cli.command("json")
@click.argument("json_file", type=click.Path(exists=True))
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Output directory. If not specified, writes to temp dir and opens in browser.",
)
@click.option(
    "-a",
    "--output-auto",
    is_flag=True,
    help="Auto-name output subdirectory based on filename (uses -o as parent, or current dir).",
)
@click.option(
    "--repo",
    help="GitHub repo (owner/name) for commit links. Auto-detected from git output if not specified.",
)
@click.option(
    "--gist",
    is_flag=True,
    help="Upload to GitHub Gist and output a gistpreview.github.io URL.",
)
@click.option(
    "--json",
    "include_json",
    is_flag=True,
    help="Include the original JSONL session file in the output directory.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the generated index.html in your default browser (default if no -o specified).",
)
def json_cmd(json_file, output, output_auto, repo, gist, include_json, open_browser):
    """Convert a Codex session JSONL file to HTML."""
    auto_open = output is None and not gist and not output_auto
    if output_auto:
        parent_dir = Path(output) if output else Path(".")
        output = parent_dir / Path(json_file).stem
    elif output is None:
        output = Path(tempfile.gettempdir()) / f"codex-session-{Path(json_file).stem}"

    output = Path(output)
    generate_html(json_file, output, github_repo=repo)

    click.echo(f"Output: {output.resolve()}")

    if include_json:
        output.mkdir(exist_ok=True)
        json_source = Path(json_file)
        json_dest = output / json_source.name
        shutil.copy(json_file, json_dest)
        json_size_kb = json_dest.stat().st_size / 1024
        click.echo(f"JSONL: {json_dest} ({json_size_kb:.1f} KB)")

    if gist:
        inject_gist_preview_js(output)
        click.echo("Creating GitHub gist...")
        gist_id, gist_url = create_gist(output)
        preview_url = f"https://gistpreview.github.io/?{gist_id}/index.html"
        click.echo(f"Gist: {gist_url}")
        click.echo(f"Preview: {preview_url}")

    if open_browser or auto_open:
        index_url = (output / "index.html").resolve().as_uri()
        webbrowser.open(index_url)


def main():
    cli()
