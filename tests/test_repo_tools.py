import hashlib
from pathlib import Path

import anyio
import pytest

from app.data.models import PublicCase
from app.mcp_servers.client import RepoMCPClient
from app.mcp_servers.models import MAX_READ_LINES, ToolErrorCode
from app.mcp_servers.repo_tools import RepoTools
from app.workspace.manager import WorkspaceManager, new_investigation_id, run_git

EXPECTED_TOOLS = {
    "list_directory",
    "search_code",
    "read_file",
    "find_symbol",
    "git_log",
    "git_show",
    "git_diff",
    "list_test_files",
}


def _case(sha: str) -> PublicCase:
    return PublicCase(
        instance_id="owner__fixture-1", repo="owner/fixture", problem_statement="p", base_commit=sha
    )


@pytest.fixture(scope="module")
def prepared(
    tmp_path_factory: pytest.TempPathFactory, fixture_repo: tuple[Path, list[str]]
) -> tuple[WorkspaceManager, str, list[str]]:
    """Workspace at the *buggy* commit (sha[1]); full history fetched so git tools have depth."""
    origin, shas = fixture_repo
    manager = WorkspaceManager(tmp_path_factory.mktemp("ws") / "workspaces", git_timeout=60)
    inv = new_investigation_id()
    ws = manager.prepare(inv, _case(shas[1]), remote_url=origin.as_uri())
    run_git(ws.repo_path, "fetch", "--quiet", "--unshallow", "origin", timeout=60)
    return manager, inv, shas


@pytest.fixture(scope="module")
def tools(prepared: tuple[WorkspaceManager, str, list[str]]) -> RepoTools:
    manager, inv, _ = prepared
    return RepoTools.for_investigation(manager, inv)


def _snapshot(repo: Path) -> tuple[str, str, str]:
    head = run_git(repo, "rev-parse", "HEAD", timeout=30).stdout
    status = run_git(repo, "status", "--porcelain", "--ignored", timeout=30).stdout
    digest = hashlib.sha256()
    for path in sorted(p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts):
        digest.update(path.relative_to(repo).as_posix().encode() + path.read_bytes())
    return head, status, digest.hexdigest()


# --- binding ---------------------------------------------------------------


def test_for_investigation_requires_prepared_workspace(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    with pytest.raises(FileNotFoundError):
        RepoTools.for_investigation(manager, new_investigation_id())


# --- list_directory --------------------------------------------------------


def test_list_directory_root_hides_git_and_env(tools: RepoTools) -> None:
    result = tools.list_directory(".")
    assert result.ok and result.evidence_id
    names = {e.name for e in result.entries}
    assert {"pkg", "tests", "module.py", "conftest.py"} <= names
    assert ".git" not in names and ".env" not in names


def test_list_directory_nested_and_errors(tools: RepoTools) -> None:
    result = tools.list_directory("pkg")
    by_name = {e.name: e for e in result.entries}
    assert by_name["core.py"].kind == "file" and by_name["core.py"].path == "pkg/core.py"
    assert tools.list_directory("missing").error.code == ToolErrorCode.NOT_FOUND
    assert tools.list_directory("module.py").error.code == ToolErrorCode.NOT_A_DIRECTORY


# --- read_file -------------------------------------------------------------


def test_read_file_returns_numbered_lines(tools: RepoTools) -> None:
    result = tools.read_file("pkg/core.py", 12, 13)
    assert result.ok and not result.truncated
    assert [line.number for line in result.lines] == [12, 13]
    assert "BUG: off by one" in result.lines[1].text


def test_read_file_enforces_line_limit(tools: RepoTools) -> None:
    result = tools.read_file("pkg/big.py", 1, 1000)
    assert len(result.lines) == MAX_READ_LINES
    assert result.end_line == MAX_READ_LINES and result.truncated
    assert result.total_lines == 450

    default = tools.read_file("pkg/big.py")
    assert len(default.lines) == MAX_READ_LINES and default.truncated

    tail = tools.read_file("pkg/big.py", 400)
    assert tail.lines[0].text == "LINE_400 = 400" and len(tail.lines) == 51
    assert not tail.truncated


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("nope.py", ToolErrorCode.NOT_FOUND),
        ("pkg", ToolErrorCode.NOT_A_FILE),
        ("pkg/blob.bin", ToolErrorCode.BINARY_FILE),
        ("../outside.txt", ToolErrorCode.PATH_REJECTED),
        ("/etc/passwd", ToolErrorCode.PATH_REJECTED),
        ("pkg/../../x", ToolErrorCode.PATH_REJECTED),
        (".git/config", ToolErrorCode.PATH_REJECTED),
        (".env", ToolErrorCode.PATH_REJECTED),
    ],
)
def test_read_file_typed_errors(tools: RepoTools, path: str, code: ToolErrorCode) -> None:
    result = tools.read_file(path)
    assert not result.ok and result.error.code == code
    assert result.lines == [] and result.evidence_id is None


def test_read_file_invalid_range(tools: RepoTools) -> None:
    assert tools.read_file("module.py", 0).error.code == ToolErrorCode.INVALID_ARGUMENT
    assert tools.read_file("module.py", 5, 2).error.code == ToolErrorCode.INVALID_ARGUMENT


def test_symlink_escape_rejected(tmp_path: Path, fixture_repo: tuple[Path, list[str]]) -> None:
    origin, shas = fixture_repo
    manager = WorkspaceManager(tmp_path / "workspaces", git_timeout=60)
    inv = new_investigation_id()
    ws = manager.prepare(inv, _case(shas[1]), remote_url=origin.as_uri())
    (tmp_path / "secret.txt").write_text("secret")
    (ws.repo_path / "escape").symlink_to(tmp_path / "secret.txt")
    result = RepoTools.for_investigation(manager, inv).read_file("escape")
    assert result.error.code == ToolErrorCode.PATH_REJECTED


# --- search_code / find_symbol ---------------------------------------------


def test_search_code_finds_matches(tools: RepoTools) -> None:
    result = tools.search_code("compute_total")
    paths = {m.path for m in result.matches}
    assert {"pkg/core.py", "pkg/usage.py", "docs/readme.md"} <= paths
    assert all(m.line > 0 for m in result.matches)


def test_search_code_glob_and_cap(tools: RepoTools) -> None:
    only_py = tools.search_code("compute_total", glob="**/*.py")
    assert only_py.matches and all(m.path.endswith(".py") for m in only_py.matches)

    capped = tools.search_code("LINE_", max_results=5)
    assert len(capped.matches) == 5 and capped.truncated


def test_search_code_no_match_and_validation(tools: RepoTools) -> None:
    empty = tools.search_code("definitely_not_present_xyz")
    assert empty.ok and empty.matches == []
    assert tools.search_code("").error.code == ToolErrorCode.INVALID_ARGUMENT
    assert tools.search_code("x", max_results=1000).error.code == ToolErrorCode.INVALID_ARGUMENT
    for glob in ["../*", "/etc/*", ":(top)*", "-x"]:
        assert tools.search_code("x", glob=glob).error.code == ToolErrorCode.INVALID_ARGUMENT


def test_search_code_treats_option_like_query_as_text(tools: RepoTools) -> None:
    result = tools.search_code("--output=/tmp/x")
    assert result.ok and result.matches == []


def test_find_symbol_definitions_only(tools: RepoTools) -> None:
    result = tools.find_symbol("compute_total")
    assert [(m.path, m.kind) for m in result.matches] == [("pkg/core.py", "function")]

    assert tools.find_symbol("Widget").matches[0].kind == "class"
    assert tools.find_symbol("fetch_data").matches[0].kind == "function"
    assert tools.find_symbol("DEFAULT_LIMIT").matches[0].kind == "assignment"
    assert tools.find_symbol("limit_hint").matches[0].kind == "assignment"
    assert tools.find_symbol("bad name").error.code == ToolErrorCode.INVALID_ARGUMENT


# --- git tools -------------------------------------------------------------


def test_git_log(tools: RepoTools, prepared: tuple[WorkspaceManager, str, list[str]]) -> None:
    _, _, shas = prepared
    result = tools.git_log()
    assert [c.sha for c in result.commits] == [shas[1], shas[0]]
    assert result.commits[0].subject == "commit 1"
    assert len(tools.git_log(max_count=1).commits) == 1
    assert tools.git_log(path="pkg/core.py").commits[0].sha == shas[0]
    assert tools.git_log(max_count=0).error.code == ToolErrorCode.INVALID_ARGUMENT
    assert tools.git_log(path="../x").error.code == ToolErrorCode.PATH_REJECTED


def test_git_show(tools: RepoTools, prepared: tuple[WorkspaceManager, str, list[str]]) -> None:
    _, _, shas = prepared
    result = tools.git_show("HEAD", path="module.py")
    assert result.ok and "+VERSION = 1" in result.text
    assert tools.git_show(shas[0][:10]).ok
    for bad in ["--output=/tmp/x", "main; rm", "HEAD..x", "f" * 41]:
        assert tools.git_show(bad).error.code == ToolErrorCode.INVALID_ARGUMENT
    assert tools.git_show("abcdef1234").error.code == ToolErrorCode.COMMAND_FAILED


def test_git_diff(tools: RepoTools, prepared: tuple[WorkspaceManager, str, list[str]]) -> None:
    _, _, shas = prepared
    assert tools.git_diff().text == ""  # clean working tree
    between = tools.git_diff(shas[0], shas[1], path="module.py")
    assert "-VERSION = 0" in between.text and "+VERSION = 1" in between.text
    assert tools.git_diff("-p").error.code == ToolErrorCode.INVALID_ARGUMENT


def test_list_test_files(tools: RepoTools) -> None:
    result = tools.list_test_files()
    assert result.files == [
        "conftest.py",
        "pkg/core_test.py",
        "tests/helpers.py",
        "tests/test_core.py",
    ]
    assert tools.list_test_files("tests").files == ["tests/helpers.py", "tests/test_core.py"]
    assert tools.list_test_files("../").error.code == ToolErrorCode.PATH_REJECTED


def test_evidence_ids_are_deterministic(tools: RepoTools) -> None:
    a = tools.read_file("module.py")
    b = tools.read_file("module.py")
    c = tools.read_file("pkg/core.py")
    assert a.evidence_id == b.evidence_id != c.evidence_id


def test_tools_do_not_mutate_workspace(
    tools: RepoTools, prepared: tuple[WorkspaceManager, str, list[str]]
) -> None:
    before = _snapshot(tools.repo)
    tools.list_directory(".")
    tools.read_file("pkg/core.py")
    tools.search_code("compute_total")
    tools.find_symbol("Widget")
    tools.git_log()
    tools.git_show("HEAD")
    tools.git_diff()
    tools.list_test_files()
    assert _snapshot(tools.repo) == before


# --- MCP integration -------------------------------------------------------


def test_mcp_server_end_to_end(
    prepared: tuple[WorkspaceManager, str, list[str]],
) -> None:
    manager, inv, shas = prepared

    async def scenario() -> None:
        with anyio.fail_after(60):
            async with RepoMCPClient.connect(inv, manager.root) as client:
                assert set(await client.list_tool_names()) == EXPECTED_TOOLS

                found = await client.find_symbol("compute_total")
                assert found.ok and found.matches[0].path == "pkg/core.py"

                read = await client.read_file("pkg/core.py", found.matches[0].line, 13)
                assert read.ok and read.evidence_id
                assert any("BUG: off by one" in line.text for line in read.lines)

                log = await client.git_log(max_count=5)
                assert log.commits[0].sha == shas[1]

                tests = await client.list_test_files()
                assert "tests/test_core.py" in tests.files

                listing = await client.list_directory("pkg")
                assert listing.ok

                search = await client.search_code("compute_total", glob="**/*.py")
                assert search.ok and search.matches

                assert (await client.git_show("HEAD", "module.py")).ok
                assert (await client.git_diff()).ok

                denied = await client.read_file("../../etc/passwd")
                assert not denied.ok and denied.error.code == ToolErrorCode.PATH_REJECTED

                bad_args = await client.read_file("module.py", start_line="x")  # type: ignore[arg-type]
                assert not bad_args.ok

    anyio.run(scenario)


def test_mcp_server_refuses_unprepared_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        with anyio.fail_after(30):
            async with RepoMCPClient.connect(new_investigation_id(), tmp_path / "workspaces"):
                pass

    with pytest.raises(Exception):  # noqa: B017 - server exits before initialize
        anyio.run(scenario)
