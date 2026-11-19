"""Branch review extension for Fava.

Lists the transactions that a git branch changes relative to ``origin/main``
in the ledger's beancount repository, shows each transaction's source
documents inline, and lets the user approve or comment on each one.
Review state (approvals and comments) is stored as JSON outside the
repository, in ``~/.branch-review/<branch>.json``. Per-item notes
written by Muse (a link to where the receipt can be fetched, and
questions that should not clutter the transaction text) live in a
sibling file, ``~/.branch-review/<branch>.annotations.json``, keyed
by item key. The page's "Submit to Muse" button writes a snapshot
of the review to ``~/.branch-review/<branch>.submitted.json`` for
Muse to process. When the repository has a ``gitea-push`` remote, the
branch's pull-request number is looked up through the Gitea API so
the page can link straight to the PR.

Enable in a beancount file with::

    2026-01-01 custom "fava-extension" "fava.ext.branch_review"

An optional config dict can point at the repository explicitly (by
default the repository is the parent of the ``b/`` folder containing
the ledger file)::

    2026-01-01 custom "fava-extension" "fava.ext.branch_review" "{'repo_path': '/home/muse/bc'}"
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from flask import Response
from markupsafe import Markup
from markupsafe import escape
from flask import jsonify
from flask import request

from fava.ext import extension_endpoint
from fava.ext import FavaExtensionBase

if TYPE_CHECKING:  # pragma: no cover
    from typing import Any

HEADER_RE = re.compile(r'^(\S+) [!*] "([^"]*)" "([^"]*)"')
DOC_RE = re.compile(
    r'^\s*(?:doc-\d+|document-\d+|document-s\d*|document):\s+"([^"]+)"',
    re.M,
)

MIMETYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _blocks(text: str) -> list[str]:
    """Split beancount file text into date-led directive blocks."""
    out = []
    for block in re.split(r"\n\s*\n", text.strip("\n")):
        if not block.strip():
            continue
        first = block.splitlines()[0].strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}\b", first):
            out.append(block)
    return out


def _header_key(block: str) -> str:
    return block.splitlines()[0].strip()


def _date_narr_key(block: str) -> tuple[str, str | None] | None:
    first = block.splitlines()[0].strip()
    match = re.match(r'^(\d{4}-\d{2}-\d{2})\s+[!*]\s+(?:"[^"]*"\s+)?"([^"]*)"', first)
    if match:
        return (match.group(1), match.group(2))
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", first)
    return (match.group(1), None) if match else None


class BranchReview(FavaExtensionBase):
    """Review the changed transactions on a git branch."""

    report_title = "Branch Review"

    has_js_module = True

    # ------------------------------------------------------------------
    # repository helpers
    # ------------------------------------------------------------------
    def repo_path(self) -> Path:
        """Git repository that contains the ledger file."""
        if isinstance(self.config, dict) and self.config.get("repo_path"):
            return Path(self.config["repo_path"])
        return Path(self.ledger.beancount_file_path).resolve().parent.parent

    def _git(self, *args: str, text: bool = True) -> str | bytes:
        result = subprocess.run(
            ["git", "-C", str(self.repo_path()), *args],
            capture_output=True,
            text=text,
            check=False,
        )
        return result.stdout

    def branches(self) -> list[str]:
        """Local branches with changes that are not yet in origin/main.

        Uses ``git cherry`` (patch equivalence), so branches whose PR was
        squash- or rebase-merged are excluded even though their commits
        are not ancestors of main. Newest branch tip first, so the
        current PR branch is the default.
        """
        lines = self._git(
            "branch", "--format=%(refname:short) %(committerdate:unix)"
        ).splitlines()
        out = []
        for line in lines:
            name, _, stamp = line.rpartition(" ")
            if not name:
                continue
            cherry = self._git("cherry", "origin/main", name)
            if any(line.startswith("+") for line in cherry.splitlines()):
                out.append((int(stamp or 0), name))
        out.sort(reverse=True)
        return [name for _, name in out]

    def pick_branch(self, requested: str | None) -> str | None:
        """The requested branch if valid, else the only candidate."""
        candidates = self.branches()
        if requested in candidates:
            return requested
        if len(candidates) == 1:
            return candidates[0]
        return candidates[0] if candidates else None

    # ------------------------------------------------------------------
    # changed transactions
    # ------------------------------------------------------------------
    def _resolve_doc(self, value: str, book: str, by_base: dict) -> str | None:
        if value.startswith("/"):
            return "f" + value
        if "/" in value:
            return "f/" + value
        cands = by_base.get(value, [])
        if not cands:
            return None
        same = [c for c in cands if c.startswith(f"f/{book}/")]
        return (same or cands)[0]

    def review_items(self, branch: str) -> list[dict[str, Any]]:
        """Transactions added or modified on branch vs origin/main."""
        head = self._git("rev-parse", branch).strip()
        cache = getattr(self, "_items_cache", None)
        if cache is None:
            cache = self._items_cache = {}
        if cache.get("key") == (branch, head):
            return cache["items"]

        tree = self._git("ls-tree", "-r", "--name-only", branch).splitlines()
        by_base: dict[str, list[str]] = {}
        for path in tree:
            by_base.setdefault(path.rsplit("/", 1)[-1], []).append(path)

        items: list[dict[str, Any]] = []
        changed = self._git(
            "diff", "--name-only", f"origin/main...{branch}", "--", "b/*.bean",
        ).split()
        # Compare block contents against the merge-base, not current
        # origin/main: once other work merges, main's newer versions of
        # blocks this branch never touched would otherwise show up here
        # as phantom changes (in reverse).
        base = self._git("merge-base", "origin/main", branch).strip()
        for path in sorted(changed):
            bookfile = path.rsplit("/", 1)[-1].replace(".bean", "")
            book = bookfile[:2]
            old_blocks = _blocks(self._git("show", f"{base}:{path}"))
            new_blocks = _blocks(self._git("show", f"{branch}:{path}"))

            old_set: dict[str, int] = {}
            for block in old_blocks:
                old_set[block] = old_set.get(block, 0) + 1
            added = []
            for block in new_blocks:
                if old_set.get(block, 0) > 0:
                    old_set[block] -= 1
                else:
                    added.append(block)
            new_set: dict[str, int] = {}
            for block in new_blocks:
                new_set[block] = new_set.get(block, 0) + 1
            removed = []
            for block in old_blocks:
                if new_set.get(block, 0) > 0:
                    new_set[block] -= 1
                else:
                    removed.append(block)

            # pair added/removed as Modified: same header, else date+narration
            modified: set[int] = set()
            used_removed: set[int] = set()
            for idx, block in enumerate(added):
                pair = None
                for i, old in enumerate(removed):
                    if i not in used_removed and _header_key(old) == _header_key(block):
                        pair = i
                        break
                if pair is None:
                    key = _date_narr_key(block)
                    if key and key[1] is not None:
                        for i, old in enumerate(removed):
                            if i not in used_removed and _date_narr_key(old) == key:
                                pair = i
                                break
                if pair is not None:
                    used_removed.add(pair)
                    modified.add(idx)

            for idx, block in enumerate(added):
                header = HEADER_RE.match(block.splitlines()[0].strip())
                date, payee, narration = (
                    header.groups() if header else (block.splitlines()[0], "", "")
                )
                docs = []
                for value in DOC_RE.findall(block):
                    resolved = self._resolve_doc(value, book, by_base)
                    if resolved:
                        suffix = Path(resolved).suffix.lower()
                        docs.append(
                            {
                                "path": resolved,
                                "name": resolved.rsplit("/", 1)[-1],
                                "is_pdf": suffix == ".pdf",
                                "is_image": suffix in (".jpg", ".jpeg", ".png"),
                            }
                        )
                key_src = f"{bookfile}|{date}|{payee}|{narration}|{block}"
                items.append(
                    {
                        "key": hashlib.sha1(key_src.encode()).hexdigest()[:12],
                        "book": bookfile,
                        "kind": "Modified" if idx in modified else "Added",
                        "date": date,
                        "payee": payee,
                        "narration": narration,
                        "has_question": any("QUESTION" in line for line in block.splitlines()),
                        "html": Markup(
                            "\n".join(
                                f'<span class="br-q">{escape(line)}</span>'
                                if "QUESTION" in line
                                else str(escape(line))
                                for line in block.splitlines()
                            )
                        ),
                        "docs": docs,
                    }
                )

        cache["key"] = (branch, head)
        cache["items"] = items
        return items

    def review_data(self, requested: str | None) -> dict[str, Any]:
        """Everything the report template needs."""
        branches = self.branches()
        branch = self.pick_branch(requested)
        if branch is None:
            return {
                "branches": branches,
                "branch": None,
                "items": [],
                "head": "",
                "pr": None,
                "branch_prs": {},
                "submission": None,
            }
        items = self.review_items(branch)
        annotations = self._load_annotations(branch)
        for item in items:
            note = annotations.get(item["key"], {})
            item["receipt_url"] = note.get("receipt_url", "")
            item["receipt_label"] = note.get("receipt_label", "")
            item["questions"] = note.get("questions", "")
        prs = self.pr_map()
        number = prs.get(branch)
        return {
            "branches": branches,
            "branch": branch,
            "head": self._git("rev-parse", "--short", branch).strip(),
            "items": items,
            "branch_prs": prs,
            "pr": (
                {"number": number, "url": self._pr_url(number)}
                if number
                else None
            ),
            "submission": self._load_submission(branch),
        }

    # ------------------------------------------------------------------
    # annotations (receipt links and questions), stored outside the repo
    # ------------------------------------------------------------------
    def _annotations_file(self, branch: str) -> Path:
        folder = Path.home() / ".branch-review"
        folder.mkdir(exist_ok=True)
        return folder / (branch.replace("/", "_") + ".annotations.json")

    def _load_annotations(self, branch: str) -> dict[str, Any]:
        """Per-item notes written by Muse: receipt links and questions.

        Kept apart from the review state file, which the page itself
        writes, so the two never overwrite each other.
        """
        ann_file = self._annotations_file(branch)
        if ann_file.exists():
            try:
                data = json.loads(ann_file.read_text())
                if isinstance(data, dict):
                    return data
            except (ValueError, OSError):
                pass
        return {}

    # ------------------------------------------------------------------
    # Gitea pull requests, so each branch can link to its PR
    # ------------------------------------------------------------------
    def _gitea(self) -> tuple[str, str, str] | None:
        """(base url, owner/repo, token) parsed from the push remote."""
        url = self._git("remote", "get-url", "gitea-push").strip()
        match = re.match(
            r"^(https?://)(?:[^:@/]+:)?([^:@/]+)@([^/]+)/(.+?)(?:\.git)?$",
            url,
        )
        if not match:
            return None
        scheme, token, host, repo = match.groups()
        return f"{scheme}{host}", repo, token

    def pr_map(self) -> dict[str, int]:
        """Branch name -> PR number, from the Gitea API (cached 2 min)."""
        cache = getattr(self, "_pr_cache", None)
        now = time.time()
        if cache and now - cache["when"] < 120:
            return cache["map"]
        mapping: dict[str, int] = {}
        gitea = self._gitea()
        if gitea:
            base, repo, token = gitea
            try:
                req = urllib.request.Request(
                    f"{base}/api/v1/repos/{repo}/pulls?state=all&limit=50",
                    headers={"Authorization": f"token {token}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    for pull in json.loads(resp.read()):
                        ref = pull.get("head", {}).get("ref", "")
                        if ref and ref not in mapping:
                            mapping[ref] = int(pull["number"])
            except Exception:  # noqa: BLE001 - the link is a convenience
                mapping = {}
        self._pr_cache = {"when": now, "map": mapping}
        return mapping

    def _pr_url(self, number: int) -> str:
        gitea = self._gitea()
        if not gitea:
            return ""
        base, repo, _ = gitea
        return f"{base}/{repo}/pulls/{number}"

    # ------------------------------------------------------------------
    # review state (approvals and comments), stored outside the repo
    # ------------------------------------------------------------------
    def _state_file(self, branch: str) -> Path:
        folder = Path.home() / ".branch-review"
        folder.mkdir(exist_ok=True)
        return folder / (branch.replace("/", "_") + ".json")

    def _load_state(self, branch: str) -> dict[str, Any]:
        state_file = self._state_file(branch)
        if state_file.exists():
            return json.loads(state_file.read_text())
        return {}

    @extension_endpoint
    def state(self) -> Response:
        """GET endpoint: saved review state for a branch."""
        branch = request.args.get("branch", "")
        return jsonify(self._load_state(branch) if branch else {})

    @extension_endpoint("save", ["POST"])
    def save(self) -> Response:
        """POST endpoint: save one item's status, comment, collapse state."""
        data = request.get_json(force=True)
        branch = str(data.get("branch", ""))
        key = str(data.get("key", ""))
        if branch not in self.branches() or not key:
            return jsonify({"ok": False})
        state = self._load_state(branch)
        entry = state.get(key, {})
        if "approved" in data:
            entry["approved"] = bool(data["approved"])
        if "status" in data:
            entry["status"] = str(data["status"])
        if "comment" in data:
            entry["comment"] = str(data["comment"])
        if "collapsed" in data:
            entry["collapsed"] = bool(data["collapsed"])
        state[key] = entry
        self._state_file(branch).write_text(json.dumps(state, indent=1))
        return jsonify({"ok": True})

    # ------------------------------------------------------------------
    # submissions: Marvin signals Muse to process a finished review
    # ------------------------------------------------------------------
    def _submitted_file(self, branch: str) -> Path:
        folder = Path.home() / ".branch-review"
        folder.mkdir(exist_ok=True)
        return folder / (branch.replace("/", "_") + ".submitted.json")

    def _load_submission(self, branch: str) -> dict[str, Any] | None:
        """The pending submission for a branch, if one is waiting."""
        sub_file = self._submitted_file(branch)
        if sub_file.exists():
            try:
                data = json.loads(sub_file.read_text())
                if isinstance(data, dict):
                    return data
            except (ValueError, OSError):
                pass
        return None

    @extension_endpoint("submit", ["POST"])
    def submit(self) -> Response:
        """POST endpoint: snapshot the review and flag it for Muse.

        Writes ``~/.branch-review/<branch>.submitted.json`` with every
        item's status and comment as of now. A watcher on Muse's side
        picks the file up, processes the needs-work items, and archives
        the file; while it sits there, the page shows the submission as
        pending. Submitting again replaces the snapshot.
        """
        data = request.get_json(force=True)
        branch = str(data.get("branch", ""))
        if branch not in self.branches():
            return jsonify({"ok": False})
        items = self.review_items(branch)
        state = self._load_state(branch)
        snapshot = []
        counts = {"approved": 0, "needs-work": 0, "unchecked": 0}
        for item in items:
            entry = state.get(item["key"], {})
            status = entry.get("status") or (
                "approved" if entry.get("approved") else "none"
            )
            if status == "approved":
                counts["approved"] += 1
            elif status == "needs-work":
                counts["needs-work"] += 1
            else:
                counts["unchecked"] += 1
            snapshot.append(
                {
                    "key": item["key"],
                    "book": item["book"],
                    "kind": item["kind"],
                    "date": item["date"],
                    "payee": item["payee"],
                    "narration": item["narration"],
                    "status": status,
                    "comment": entry.get("comment", ""),
                }
            )
        submitted_at = datetime.now().astimezone().isoformat(timespec="seconds")
        submission = {
            "id": hashlib.sha1(
                f"{branch}|{submitted_at}".encode()
            ).hexdigest()[:12],
            "branch": branch,
            "head": self._git("rev-parse", "--short", branch).strip(),
            "pr": self.pr_map().get(branch),
            "submitted_at": submitted_at,
            "counts": counts,
            "items": snapshot,
        }
        self._submitted_file(branch).write_text(json.dumps(submission, indent=1))
        return jsonify(
            {"ok": True, "submitted_at": submitted_at, "counts": counts}
        )

    # ------------------------------------------------------------------
    # document serving: file bytes out of the branch's git tree
    # ------------------------------------------------------------------
    @extension_endpoint
    def doc(self) -> Response:
        """GET endpoint: a document file as of the branch."""
        branch = request.args.get("branch", "")
        path = request.args.get("path", "")
        if branch not in self.branches() or not path.startswith("f/"):
            return Response("not found", status=404)
        tree = self._git("ls-tree", "-r", "--name-only", branch).splitlines()
        if path not in tree:
            return Response("not found", status=404)
        data = self._git("cat-file", "-p", f"{branch}:{path}", text=False)
        mimetype = MIMETYPES.get(Path(path).suffix.lower(), "application/octet-stream")
        return Response(data, mimetype=mimetype)
