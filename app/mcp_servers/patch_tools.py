"""Privileged patch application, bound to one investigation workspace.

This is the only code path that mutates a workspace repository. It accepts no
diff from its caller: it applies the patch persisted with the investigation, and
only after checking the store for an `approve` decision bound to that exact patch
(sha256) while the investigation is PATCHING. It never commits or pushes.
"""

from pathlib import Path

from app.agent.models import ProposedPatch
from app.agent.patches import patch_sha256
from app.mcp_servers.models import ApplyPatchResult, ToolErrorCode
from app.mcp_servers.repo_tools import GIT_APPLY_OPTIONS, RepoTools, ToolFailure
from app.storage.models import ApprovalDecision, InvestigationStatus
from app.storage.sqlite import InvestigationNotFoundError, InvestigationStore
from app.workspace.manager import WorkspaceManager


class PatchTools:
    def __init__(
        self,
        repo: RepoTools,
        store: InvestigationStore,
        investigation_id: str,
        patches_dir: Path,
    ) -> None:
        self.repo = repo
        self.store = store
        self.investigation_id = investigation_id
        self.patches_dir = patches_dir

    @classmethod
    def for_investigation(
        cls, manager: WorkspaceManager, store: InvestigationStore, investigation_id: str
    ) -> "PatchTools":
        """Bind to a prepared, isolated workspace (never the BugTrace source tree)."""
        repo = RepoTools.for_investigation(manager, investigation_id)
        patches_dir = manager.workspace_dir(investigation_id) / "patches"
        return cls(repo, store, investigation_id, patches_dir)

    def authorize(self) -> ProposedPatch:
        """Return the approved patch, or raise NOT_AUTHORIZED."""

        def deny(reason: str) -> ToolFailure:
            return ToolFailure(ToolErrorCode.NOT_AUTHORIZED, f"apply_patch refused: {reason}")

        try:
            stored = self.store.get(self.investigation_id)
        except InvestigationNotFoundError as exc:
            raise deny("unknown investigation") from exc
        if stored.status != InvestigationStatus.PATCHING:
            raise deny(f"investigation is {stored.status}, not PATCHING")
        approval = stored.approval
        if approval is None or approval.decision != ApprovalDecision.APPROVE:
            raise deny("no approval recorded")
        patch = stored.record.proposed_patch if stored.record else None
        if patch is None:
            raise deny("no proposed patch")
        if not (patch_sha256(patch.diff) == patch.sha256 == approval.patch_sha256):
            raise deny("patch does not match the approved patch")
        return patch

    def apply_patch(self) -> ApplyPatchResult:
        def body() -> ApplyPatchResult:
            patch = self.authorize()
            parsed = self.repo.validate_patch(patch.diff)  # paths + `git apply --check`
            if parsed.diff != patch.diff:
                raise ToolFailure(ToolErrorCode.PATCH_REJECTED, "stored patch is not normalized")
            self.patches_dir.mkdir(parents=True, exist_ok=True)
            patch_file = self.patches_dir / f"{patch.sha256}.diff"
            patch_file.write_text(patch.diff, encoding="utf-8")
            self.repo._git("apply", *GIT_APPLY_OPTIONS, str(patch_file))
            stat = self.repo._git("diff", "--no-color", "--stat").stdout
            diff = self.repo._git("diff", "--no-color", "--no-ext-diff").stdout
            return ApplyPatchResult(applied=True, files=parsed.files, stat=stat, diff=diff)

        args = {"investigation_id": self.investigation_id}
        return self.repo._run("apply_patch", args, ApplyPatchResult, body)
