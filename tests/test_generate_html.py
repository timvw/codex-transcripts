"""Tests for HTML generation from Codex session JSONL."""

import tempfile
from pathlib import Path

import pytest

from codex_transcripts import (
    analyze_conversation,
    detect_github_repo,
    format_json,
    format_tool_stats,
    generate_html,
    get_session_summary,
    is_json_like,
    parse_session_file,
    render_content_block,
    render_markdown_text,
)


@pytest.fixture
def sample_session_path():
    return Path(__file__).parent / "sample_session.jsonl"


@pytest.fixture
def output_dir():
    """Create a temporary output directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestSessionParsing:
    def test_parse_session_file(self, sample_session_path):
        entries = parse_session_file(sample_session_path)
        assert len(entries) == 9

    def test_get_session_summary(self, sample_session_path):
        summary = get_session_summary(sample_session_path)
        assert summary == "Create a hello world function"


class TestGenerateHtml:
    def test_generates_index_html(self, output_dir, sample_session_path):
        generate_html(sample_session_path, output_dir, github_repo="example/project")
        index_html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert "Codex transcript" in index_html
        assert "Create a hello world function" in index_html

    def test_generates_page_001_html(self, output_dir, sample_session_path):
        generate_html(sample_session_path, output_dir, github_repo="example/project")
        page_html = (output_dir / "page-001.html").read_text(encoding="utf-8")
        assert "Codex transcript" in page_html
        assert "Done! I added the function." in page_html


class TestRenderHelpers:
    def test_render_markdown_text(self):
        result = render_markdown_text("**bold** and `code`\n\n- item 1\n- item 2")
        assert "<strong>bold</strong>" in result
        assert "<li>item 1</li>" in result

    def test_format_json(self):
        result = format_json({"key": "value", "number": 42, "nested": {"a": 1}})
        assert "&quot;key&quot;" in result
        assert "&quot;value&quot;" in result

    def test_is_json_like(self):
        assert is_json_like('{"key": "value"}')
        assert is_json_like("[1, 2, 3]")
        assert not is_json_like("plain text")
        assert not is_json_like("")
        assert not is_json_like(None)

    def test_render_content_block_tool_use(self):
        block = {
            "type": "tool_use",
            "name": "Bash",
            "id": "tool-1",
            "input": {"command": "rg hello src", "description": "Search"},
        }
        rendered = render_content_block(block)
        assert "Bash" in rendered
        assert "rg hello src" in rendered

    def test_render_content_block_tool_result(self):
        block = {
            "type": "tool_result",
            "content": "[main abc1234] Add hello function\n 1 file changed",
            "is_error": False,
        }
        rendered = render_content_block(block)
        assert "abc1234" in rendered


class TestAnalysis:
    def test_analyze_conversation(self):
        messages = [
            {
                "role": "assistant",
                "timestamp": "2025-12-24T10:00:06.000Z",
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {}},
                    {
                        "type": "tool_result",
                        "content": "[main abc1234] Add hello function",
                    },
                ],
            }
        ]
        stats = analyze_conversation(messages)
        assert stats["tool_counts"] == {"Bash": 1}
        assert stats["commits"][0][0] == "abc1234"

    def test_format_tool_stats(self):
        stats = format_tool_stats({"Bash": 2, "Edit": 1})
        assert stats == "2 bash · 1 edit"


class TestRepoDetect:
    def test_detect_github_repo(self):
        messages = [
            {
                "role": "assistant",
                "timestamp": "2025-12-24T10:00:06.000Z",
                "content": [
                    {
                        "type": "tool_result",
                        "content": "remote: https://github.com/example/project/pull/new/main",
                    }
                ],
            }
        ]
        assert detect_github_repo(messages) == "example/project"
