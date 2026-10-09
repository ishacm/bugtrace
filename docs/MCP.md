# MCP Tool Contracts & Safety

## V1 repository MCP server
Expose read-only repository capabilities. The LLM does not receive a general shell tool.

### `list_directory`
Input: repository-relative path.
Output: child entries.
Reject absolute paths and traversal outside workspace.

### `search_code`
Input:
- query
- optional glob
- max results (default 20, bounded)
Output: path, line, snippet.

Implementation may start with `rg`/text search. Use subprocess argument arrays with timeout.

### `read_file`
Input:
- path
- start line
- end line
Maximum 300 lines per call.
Return numbered text or typed line records.

### `find_symbol`
Input: symbol name.
V1 may use text heuristics; AST indexing is optional.

### `git_log`
Input:
- optional path
- bounded max count

### `git_show`
Input:
- commit
- optional path

### `git_diff`
Input:
- two refs when needed

### `list_test_files`
Return likely tests based on repository structure and naming.

## V2 privileged execution tools
Add separately:
- `apply_patch`
- `run_evaluation`

These tools must verify the investigation is approved before doing anything.

## Security rules
- No `shell=True`.
- No arbitrary command tool exposed to LLM.
- No reads outside the case workspace.
- No `.env` access.
- No remote Git writes.
- Every command has timeout and bounded output.
- Store tool errors as typed results rather than crashing the server.

## Patch application
Validate first:

```bash
git apply --check patch.diff
```

Only after successful validation and explicit approval:

```bash
git apply patch.diff
```

Never commit or push automatically.
