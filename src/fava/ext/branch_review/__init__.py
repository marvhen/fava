"""Branch review extension for Fava.

Lists everything a git branch changes relative to ``origin/main``
in the ledger's beancount repository: every changed beancount
directive (transactions, balance assertions, and any other dated
blocks) and every other changed file each get a review card, with
source documents shown inline, and the user can approve or comment
on each one. Dated blocks inside a ``;#region`` ... ``;#endregion``
block (the balance-assertion groups at the top of a file) are the
one exception: a changed region is reviewed as a single card
showing the whole region in context, with only the documents of
the assertions that actually changed attached. A file's card also lists the beancount entries that
reference the file, so a document changed on its own still shows
the entries it belongs to.
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
TXN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+[!*]\s")
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
    """Split beancount file text into date-led directive blocks.

    Chunks are separated by blank lines. A chunk holding a single
    directive is returned exactly as written, so existing block text
    (and the review keys derived from it) never changes. A chunk
    holding several directives with no blank lines between them --
    the balance assertions grouped inside a ;#region block are the
    common case -- is split at each date-led line, so every directive
    becomes its own block; lines before the first directive of such
    a chunk (e.g. the ;#region marker itself) are dropped.
    """
    out = []
    for chunk in re.split(r"\n\s*\n", text.strip("\n")):
        if not chunk.strip():
            continue
        current: list[str] = []
        for line in chunk.splitlines():
            if re.match(r"^\d{4}-\d{2}-\d{2}\b", line):
                if current:
                    out.append("\n".join(current))
                current = [line]
            elif current:
                current.append(line)
        if current:
            out.append("\n".join(current))
    return out


def _segments(text: str) -> list[tuple[str | None, str]]:
    """Split beancount file text into region and non-region segments.

    A region runs from a ``;#region`` line through the matching
    ``;#endregion`` line (nested regions count depth); its segment
    is keyed by the region's heading line, stripped. Everything
    else is returned as non-region segments keyed by None. Segments
    come back in file order, and joining a side's segment texts
    with newlines reproduces that side's text minus the region
    segments (for the non-region view) exactly.
    """
    segments: list[tuple[str | None, str]] = []
    buf: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^;#region(\s|$)", line):
            if buf:
                segments.append((None, "\n".join(buf)))
                buf = []
            heading = line.strip()
            region = [line]
            depth = 1
            i += 1
            while i < len(lines) and depth:
                region.append(lines[i])
                if re.match(r"^;#region(\s|$)", lines[i]):
                    depth += 1
                elif re.match(r"^;#endregion(\s|$)", lines[i]):
                    depth -= 1
                i += 1
            segments.append((heading, "\n".join(region)))
        else:
            buf.append(line)
            i += 1
    if buf:
        segments.append((None, "\n".join(buf)))
    return segments


def _added_blocks(
    old_blocks: list[str], new_blocks: list[str]
) -> tuple[list[str], set[int], list[str]]:
    """Diff two block lists as multisets.

    Returns ``(added, modified, removed)``: the blocks present in
    ``new_blocks`` but not matched in ``old_blocks``, the indexes
    within ``added`` that pair with a removed block (same header
    line, else same date and narration) and so count as modified
    rather than new, and the blocks that disappeared.
    """
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
    return added, modified, removed


def _block_html(block: str) -> Markup:
    """Render block text for a card, highlighting QUESTION lines."""
    return Markup(
        "\n".join(
            f'<span class="br-q">{escape(line)}</span>'
            if "QUESTION" in line
            else str(escape(line))
            for line in block.splitlines()
        )
    )


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
    """Review everything a git branch changes."""

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

    def _block_docs(
        self, blocks: list[str], book: str, by_base: dict
    ) -> list[dict[str, Any]]:
        """Resolved source documents of the given blocks.

        Deduped by path, first-seen order: a document shared by
        several blocks (a statement behind a whole month of balance
        assertions, say) is attached only once.
        """
        docs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for block in blocks:
            for value in DOC_RE.findall(block):
                resolved = self._resolve_doc(value, book, by_base)
                if resolved and resolved not in seen:
                    seen.add(resolved)
                    suffix = Path(resolved).suffix.lower()
                    docs.append(
                        {
                            "path": resolved,
                            "name": resolved.rsplit("/", 1)[-1],
                            "is_pdf": suffix == ".pdf",
                            "is_image": suffix in (".jpg", ".jpeg", ".png"),
                        }
                    )
        return docs

    def _region_item(
        self,
        bookfile: str,
        book: str,
        heading: str,
        new_text: str,
        old_text: str | None,
        by_base: dict,
    ) -> dict[str, Any] | None:
        """One review item for a whole ``;#region`` block, or None.

        Emitted when the region is new on the branch or any dated
        block inside it was added, removed, or modified; a region
        that exists only at the merge-base is not an item (removals
        are not reviewed), and neither is an unchanged region. The
        card shows the region's full branch-side text so the group
        is reviewed in context; only the documents of the blocks
        that actually changed are attached.
        """
        if old_text is None:
            kind = "Added"
            changed = _blocks(new_text)
        else:
            if old_text == new_text:
                return None
            added, _modified, removed = _added_blocks(
                _blocks(old_text), _blocks(new_text)
            )
            if not added and not removed:
                return None
            kind = "Modified"
            changed = added
        return {
            "key": hashlib.sha1(
                f"{bookfile}|{heading}".encode()
            ).hexdigest()[:12],
            "book": bookfile,
            "kind": kind,
            "date": "",
            "payee": "",
            "narration": "",
            "heading": heading,
            "is_region": True,
            "block": new_text,
            "has_question": any(
                "QUESTION" in line for line in new_text.splitlines()
            ),
            "html": _block_html(new_text),
            "docs": self._block_docs(changed, book, by_base),
        }

    def review_items(self, branch: str) -> list[dict[str, Any]]:
        """All changes on branch vs origin/main, as review items.

        Bean files contribute one item per added/modified directive
        block (transactions and every other dated directive, e.g.
        balance assertions) -- except blocks inside a ``;#region``
        ... ``;#endregion`` block, which contribute one item per
        changed region instead (see :meth:`_region_item`); every
        other changed file contributes one item of its own (see
        :meth:`_file_items`).
        """
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
        bean_with_items: set[str] = set()
        for path in sorted(changed):
            count_before = len(items)
            bookfile = path.rsplit("/", 1)[-1].replace(".bean", "")
            book = bookfile[:2]
            old_segments = _segments(self._git("show", f"{base}:{path}"))
            new_segments = _segments(self._git("show", f"{branch}:{path}"))
            old_regions: dict[str, str] = {}
            for heading, seg_text in old_segments:
                if heading is not None:
                    old_regions.setdefault(heading, seg_text)
            # Dated blocks outside any region keep the historical
            # per-block items and keys; blocks inside a region are
            # grouped into one item per changed region below.
            old_blocks = _blocks(
                "\n".join(t for h, t in old_segments if h is None)
            )
            new_blocks = _blocks(
                "\n".join(t for h, t in new_segments if h is None)
            )
            added, modified, _removed = _added_blocks(old_blocks, new_blocks)

            pending: list[tuple[str, dict[str, Any]]] = []
            for idx, block in enumerate(added):
                first = block.splitlines()[0].strip()
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
                if TXN_RE.match(first):
                    # Transactions keep the historical item key:
                    # review state and annotations are keyed by it.
                    header = HEADER_RE.match(first)
                    date, payee, narration = (
                        header.groups()
                        if header
                        else (block.splitlines()[0], "", "")
                    )
                    key_src = f"{bookfile}|{date}|{payee}|{narration}|{block}"
                    key = hashlib.sha1(key_src.encode()).hexdigest()[:12]
                else:
                    # Any other dated directive (balance assertions
                    # outside a region are the common case): keyed
                    # by the block alone, a formula that cannot
                    # collide with the transaction keys above.
                    date_match = re.match(r"^(\d{4}-\d{2}-\d{2})\b", first)
                    date = date_match.group(1) if date_match else first
                    payee = narration = ""
                    key = hashlib.sha1(
                        f"{bookfile}|{block}".encode()
                    ).hexdigest()[:12]
                pending.append(
                    (
                        block,
                        {
                            "key": key,
                            "book": bookfile,
                            "kind": "Modified" if idx in modified else "Added",
                            "date": date,
                            "payee": payee,
                            "narration": narration,
                            "has_question": any("QUESTION" in line for line in block.splitlines()),
                            "html": _block_html(block),
                            "docs": docs,
                        },
                    )
                )

            region_items: dict[str, dict[str, Any]] = {}
            for heading, seg_text in new_segments:
                if heading is None or heading in region_items:
                    continue
                region_item = self._region_item(
                    bookfile,
                    book,
                    heading,
                    seg_text,
                    old_regions.get(heading),
                    by_base,
                )
                if region_item is not None:
                    region_items[heading] = region_item

            # Assemble the file's items in file order: walk the
            # branch-side segments, emitting each region's item at
            # the region's position and each block item at its
            # block's position.
            for heading, seg_text in new_segments:
                if heading is not None:
                    if heading in region_items:
                        items.append(region_items.pop(heading))
                else:
                    for block in _blocks(seg_text):
                        if pending and pending[0][0] == block:
                            items.append(pending.pop(0)[1])
            # Safety net: a block item the segment walk missed
            # (possible only if a chunk spanned a segment boundary)
            # still shows up, at the end of the file's items.
            items.extend(item for _block, item in pending)
            if len(items) > count_before:
                bean_with_items.add(path)

        # A file a shown item already displays as one of its source
        # documents does not need a card of its own.
        referenced = {doc["path"] for item in items for doc in item["docs"]}
        items.extend(
            self._file_items(
                branch, base, bean_with_items, referenced, tree, by_base
            )
        )

        cache["key"] = (branch, head)
        cache["items"] = items
        return items

    def _reference_index(
        self, branch: str, tree: list[str], by_base: dict
    ) -> dict[str, list[dict[str, Any]]]:
        """Map every document path to the bean blocks that reference it.

        Parses every bean file at the branch head, not just the ones
        the branch changed, so a file card can list the entries that
        point at the file -- including entries the branch itself did
        not touch. Keys are the resolved repo-relative paths produced
        by :meth:`_resolve_doc`, the same paths bean items carry in
        their ``docs`` lists; each value holds the referencing blocks
        as ``{"book", "text", "html"}`` dicts.
        """
        index: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(tree):
            if not (path.startswith("b/") and path.endswith(".bean")):
                continue
            bookfile = path.rsplit("/", 1)[-1].replace(".bean", "")
            book = bookfile[:2]
            for block in _blocks(self._git("show", f"{branch}:{path}")):
                targets = {
                    self._resolve_doc(value, book, by_base)
                    for value in DOC_RE.findall(block)
                }
                for target in targets:
                    if target:
                        index.setdefault(target, []).append(
                            {
                                "book": bookfile,
                                "text": block,
                                "html": _block_html(block),
                            }
                        )
        return index

    def _file_items(
        self,
        branch: str,
        base: str,
        bean_with_items: set[str],
        referenced: set[str],
        tree: list[str],
        by_base: dict,
    ) -> list[dict[str, Any]]:
        """One review item per changed file not covered by bean items.

        Covers every non-bean change on the branch vs the merge-base
        (documents under ``f/``, scripts, any other file), plus bean
        files whose change produced no block items (e.g. only an
        option line changed, or the file was deleted), so that any
        change to any file shows up in the review. Files in
        ``referenced`` -- the resolved document paths of the bean
        items above -- are skipped: those items already display them.
        Files under ``f/``
        are served through the same document mechanism as transaction
        documents; other files link to their raw version on Gitea
        (the merge-base version for deleted files, which no longer
        exist at the branch head). Each item also lists the bean
        blocks that reference the file (see :meth:`_reference_index`),
        looked up under the new path and, for renames, the old one.
        """
        gitea = self._gitea()
        head = self._git("rev-parse", branch).strip()
        ref_index: dict[str, list[dict[str, Any]]] | None = None
        out: list[dict[str, Any]] = []
        diff = self._git("diff", "--name-status", base, branch)
        for line in diff.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0]
            old_path = None
            if status.startswith("R") and len(parts) >= 3:
                old_path, path = parts[1], parts[2]
                kind = "Renamed"
            else:
                path = parts[1]
                kind = {
                    "A": "Added",
                    "M": "Modified",
                    "D": "Deleted",
                }.get(status[:1], "Modified")
            is_bean = path.startswith("b/") and path.endswith(".bean")
            if is_bean and path in bean_with_items:
                continue  # its blocks are already review items
            if path in referenced:
                continue  # already shown as a document on a bean item
            docs: list[dict[str, Any]] = []
            file_url = ""
            if kind != "Deleted" and path.startswith("f/"):
                suffix = Path(path).suffix.lower()
                docs.append(
                    {
                        "path": path,
                        "name": path.rsplit("/", 1)[-1],
                        "is_pdf": suffix == ".pdf",
                        "is_image": suffix in (".jpg", ".jpeg", ".png"),
                    }
                )
            if gitea:
                base_url, repo, _token = gitea
                if kind == "Deleted":
                    file_url = f"{base_url}/{repo}/raw/commit/{base}/{path}"
                elif not docs:
                    file_url = f"{base_url}/{repo}/raw/commit/{head}/{path}"
            if ref_index is None:
                ref_index = self._reference_index(branch, tree, by_base)
            references = list(ref_index.get(path, []))
            if old_path:
                references += ref_index.get(old_path, [])
            out.append(
                {
                    "key": hashlib.sha1(
                        f"file|{path}".encode()
                    ).hexdigest()[:12],
                    "book": path,
                    "kind": kind,
                    "date": "",
                    "payee": "",
                    "narration": "",
                    "has_question": False,
                    "html": (
                        Markup(escape(f"Renamed from {old_path}"))
                        if old_path
                        else ""
                    ),
                    "docs": docs,
                    "file_url": file_url,
                    "is_file": True,
                    "references": references,
                }
            )
        out.sort(key=lambda item: item["book"])
        return out

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
            snap = {
                "key": item["key"],
                "book": item["book"],
                "kind": item["kind"],
                "date": item["date"],
                "payee": item["payee"],
                "narration": item["narration"],
                "status": status,
                "comment": entry.get("comment", ""),
            }
            if item.get("is_region"):
                # A region item stands for the whole ;#region block:
                # the snapshot carries its heading and full text so
                # the landing step can replace or insert the entire
                # region on main.
                snap["kind"] = "region"
                snap["heading"] = item["heading"]
                snap["block"] = item["block"]
            snapshot.append(snap)
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
