# codex-transcripts

[![Tests](https://github.com/timvw/codex-transcripts/actions/workflows/test.yml/badge.svg)](https://github.com/timvw/codex-transcripts/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/timvw/codex-transcripts/blob/main/LICENSE)

Convert Codex session rollouts (JSONL) into clean, mobile-friendly HTML transcripts with pagination.

## Installation

Install this tool using `uv`:
```bash
uv tool install codex-transcripts
```
Or run it without installing:
```bash
uvx codex-transcripts --help
```

## Usage

This tool converts Codex session files into browseable multi-page HTML transcripts.

There are two commands available:

- `local` (default) - select from local Codex sessions stored in `~/.codex/sessions`
- `json` - convert a specific JSONL session file

The quickest way to view a recent local session:

```bash
codex-transcripts
```

This shows an interactive picker to select a session, generates HTML, and opens it in your default browser.

### Output options

All commands support these options:

- `-o, --output DIRECTORY` - output directory (default: writes to temp dir and opens browser)
- `-a, --output-auto` - auto-name output subdirectory based on session filename
- `--repo OWNER/NAME` - GitHub repo for commit links (auto-detected from git output if not specified)
- `--open` - open the generated `index.html` in your default browser (default if no `-o` specified)
- `--gist` - upload the generated HTML files to a GitHub Gist and output a preview URL
- `--json` - include the original session file in the output directory

The generated output includes:
- `index.html` - an index page with a timeline of prompts and commits
- `page-001.html`, `page-002.html`, etc. - paginated transcript pages

### Local sessions

Local Codex sessions are stored as JSONL files under `~/.codex/sessions/YYYY/MM/DD/`. Run with no arguments to select from recent sessions:

```bash
codex-transcripts
# or explicitly:
codex-transcripts local
```

Use `--limit` to control how many sessions are shown (default: 10):

```bash
codex-transcripts local --limit 20
```

### JSONL files

Convert a specific session file directly:

```bash
codex-transcripts json ~/.codex/sessions/2025/12/25/rollout-2025-12-25T12-34-56-<uuid>.jsonl -o output-directory/
codex-transcripts json session.jsonl --open
```

### Auto-naming output directories

Use `-a/--output-auto` to automatically create a subdirectory named after the session:

```bash
# Creates ./rollout-.../ subdirectory
codex-transcripts json session.jsonl -a

# Creates ./transcripts/rollout-.../ subdirectory
codex-transcripts json session.jsonl -o ./transcripts -a
```

### Publishing to GitHub Gist

Use the `--gist` option to automatically upload your transcript to a GitHub Gist and get a shareable preview URL:

```bash
codex-transcripts --gist
codex-transcripts json session.jsonl --gist
```

This will output something like:
```
Gist: https://gist.github.com/username/abc123def456
Preview: https://gistpreview.github.io/?abc123def456/index.html
Files: /var/folders/.../session-id
```

The preview URL uses gistpreview.github.io to render your HTML gist. The tool automatically injects JavaScript to fix relative links when served through gistpreview.

Combine with `-o` to keep a local copy:

```bash
codex-transcripts json session.jsonl -o ./my-transcript --gist
```

**Requirements:** The `--gist` option requires the GitHub CLI (`gh`) to be installed and authenticated (`gh auth login`).

### Including the source file

Use the `--json` option to include the original session file in the output directory:

```bash
codex-transcripts json session.jsonl -o ./my-transcript --json
```

This will output:
```
JSONL: ./my-transcript/rollout-....jsonl (245.3 KB)
```

This is useful for archiving the source data alongside the HTML output.

## Development

To contribute to this tool, first checkout the code. You can run the tests using `uv run`:
```bash
cd codex-transcripts
uv run pytest
```

And run your local development copy of the tool like this:
```bash
uv run codex-transcripts --help
```
