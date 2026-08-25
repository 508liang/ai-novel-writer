"""Project storage and recovery primitives for the novel writing system.

The original application stored most state in a handful of module globals.  That
worked for a single project, but made project switching and crash recovery
fragile.  This module deliberately contains no GUI or model code; it is the
small, synchronous persistence boundary shared by the desktop UI and the
writing agents.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = 2
PROJECT_MANIFEST = "project.json"
CHECKPOINT_FILE = "checkpoint.json"
CHECKPOINT_DRAFT = "checkpoint_draft.md"
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def now_iso() -> str:
    """Return a local, sortable timestamp."""

    return datetime.now().astimezone().isoformat(timespec="seconds")


def count_story_chars(text: str) -> int:
    """Count visible story characters, excluding whitespace."""

    return len(re.sub(r"\s+", "", text or ""))


def safe_filename(value: str, fallback: str = "未命名") -> str:
    """Make a human-readable event name safe for Windows and POSIX paths."""

    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = (cleaned or fallback)[:100]
    if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned


def chapter_filename(number: int, event_name: str) -> str:
    """Build the canonical chapter filename used by the legacy format."""

    return f"ch{int(number):03d}_{safe_filename(event_name)}.md"


def event_identifier(event: Dict[str, Any]) -> Any:
    """Return an event's stable index, supporting both legacy id and index."""
    index = event.get("index")
    return index if index is not None and str(index) != "" else event.get("id")


def validate_project_name(name: str) -> str:
    """Validate a project folder name without silently changing user input."""

    value = (name or "").strip()
    if not value or value in {".", ".."}:
        raise ValueError("项目名称不能为空")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("项目名称不能包含控制字符")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', value):
        raise ValueError("项目名称包含 Windows 不允许的字符")
    if value.rstrip(" .") != value:
        raise ValueError("项目名称不能以空格或句点结尾")
    if value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise ValueError("项目名称不能使用 Windows 保留设备名")
    if len(value) > 100:
        raise ValueError("项目名称不能超过 100 个字符")
    return value


def atomic_write_text(path: Path, content: str, *, backup: bool = False) -> Path:
    """Write UTF-8 text atomically, optionally keeping one recoverable backup."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if backup and path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()
    return path


def atomic_write_json(path: Path, data: Any, *, backup: bool = False) -> Path:
    """Serialize JSON and persist it through :func:`atomic_write_text`."""

    return atomic_write_text(
        Path(path),
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        backup=backup,
    )


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON without turning a damaged optional file into a crash."""

    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _default_manifest(name: str, premise: str = "") -> Dict[str, Any]:
    timestamp = now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "id": uuid.uuid4().hex,
        "name": name,
        "premise": premise,
        "status": "draft",
        "created_at": timestamp,
        "updated_at": timestamp,
        "progress": {
            "current_volume": 1,
            "completed_events": 0,
            "completed_chapters": 0,
            "total_words": 0,
        },
    }


class ProjectStore:
    """Filesystem-backed storage for one novel project.

    Existing projects are intentionally not migrated or renamed.  Calling
    ``ensure_structure`` only creates missing directories and a small metadata
    file, so an existing ``chapters/`` or ``story_plan.md`` remains untouched.
    """

    REQUIRED_DIRS = ("chapters", "raw", "logs", "outline", "backups")

    def __init__(self, project_dir: Path | str):
        self.project_dir = Path(project_dir).expanduser()
        self.manifest_path = self.project_dir / PROJECT_MANIFEST
        self.checkpoint_path = self.project_dir / CHECKPOINT_FILE
        self.checkpoint_draft_path = self.project_dir / CHECKPOINT_DRAFT

    @property
    def chapters_dir(self) -> Path:
        return self.project_dir / "chapters"

    @property
    def raw_dir(self) -> Path:
        return self.project_dir / "raw"

    @property
    def logs_dir(self) -> Path:
        return self.project_dir / "logs"

    @property
    def events_path(self) -> Path:
        return self.project_dir / "events_config.json"

    @property
    def config_path(self) -> Path:
        return self.project_dir / "config.json"

    def ensure_structure(self, *, name: Optional[str] = None, premise: str = "") -> Path:
        """Create missing project folders and metadata."""

        self.project_dir.mkdir(parents=True, exist_ok=True)
        for directory in self.REQUIRED_DIRS:
            (self.project_dir / directory).mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.exists():
            project_name = validate_project_name(name or self.project_dir.name)
            atomic_write_json(self.manifest_path, _default_manifest(project_name, premise))
        else:
            # Backfill only missing metadata fields; preserve user edits.
            manifest = self.load_manifest()
            changed = False
            defaults = _default_manifest(name or self.project_dir.name, premise)
            for key, value in defaults.items():
                if key not in manifest:
                    manifest[key] = value
                    changed = True
            if changed:
                manifest["updated_at"] = now_iso()
                atomic_write_json(self.manifest_path, manifest)
        return self.project_dir

    def load_manifest(self) -> Dict[str, Any]:
        data = read_json(self.manifest_path, {})
        return data if isinstance(data, dict) else {}

    def update_manifest(self, **changes: Any) -> Dict[str, Any]:
        manifest = self.load_manifest()
        if not manifest:
            self.ensure_structure()
            manifest = self.load_manifest()
        for key, value in changes.items():
            manifest[key] = value
        try:
            existing_version = int(manifest.get("schema_version", 1))
        except (TypeError, ValueError):
            existing_version = 1
        manifest["schema_version"] = max(existing_version, SCHEMA_VERSION)
        manifest["updated_at"] = now_iso()
        atomic_write_json(self.manifest_path, manifest, backup=True)
        return manifest

    def load_config(self) -> Dict[str, Any]:
        data = read_json(self.config_path, {})
        return data if isinstance(data, dict) else {}

    def save_config(self, config: Dict[str, Any]) -> Path:
        return atomic_write_json(self.config_path, config, backup=True)

    def load_checkpoint(self) -> Optional[Dict[str, Any]]:
        data = read_json(self.checkpoint_path, None)
        return data if isinstance(data, dict) else None

    def read_checkpoint_draft(self) -> str:
        if not self.checkpoint_draft_path.exists():
            return ""
        try:
            return self.checkpoint_draft_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def save_checkpoint(self, state: Dict[str, Any], *, draft_text: Optional[str] = None) -> Dict[str, Any]:
        """Persist an active task checkpoint and optional generated draft."""

        payload = dict(state)
        payload.setdefault("checkpoint_version", 1)
        payload.setdefault("task_id", uuid.uuid4().hex)
        payload["updated_at"] = now_iso()
        if draft_text is not None:
            atomic_write_text(self.checkpoint_draft_path, draft_text)
            payload["draft_file"] = CHECKPOINT_DRAFT
            payload["draft_chars"] = count_story_chars(draft_text)
        atomic_write_json(self.checkpoint_path, payload, backup=True)
        return payload

    def clear_checkpoint(self, *, preserve_draft: bool = False) -> None:
        """Remove the active checkpoint after a task is fully committed."""

        if self.checkpoint_path.exists():
            self.checkpoint_path.unlink()
        if not preserve_draft and self.checkpoint_draft_path.exists():
            self.checkpoint_draft_path.unlink()

    def load_events(self) -> Tuple[List[Dict[str, Any]], bool]:
        """Return events and whether the source file used an object wrapper."""

        data = read_json(self.events_path, [])
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)], False
        if isinstance(data, dict):
            events = data.get("events", [])
            if isinstance(events, list):
                return [item for item in events if isinstance(item, dict)], True
        return [], False

    def save_events(self, events: Iterable[Dict[str, Any]], *, wrapped: Optional[bool] = None) -> Path:
        existing = read_json(self.events_path, [])
        if wrapped is None:
            wrapped = isinstance(existing, dict)
        event_list = list(events)
        self._assign_missing_event_indices(event_list)
        if wrapped:
            payload = dict(existing) if isinstance(existing, dict) else {}
            payload["events"] = event_list
        else:
            payload = event_list
        return atomic_write_json(self.events_path, payload, backup=True)

    @staticmethod
    def _assign_missing_event_indices(events: List[Dict[str, Any]]) -> bool:
        """Give legacy events without an id/index a stable one-based index."""
        used = set()
        for event in events:
            value = event_identifier(event)
            if value is None or str(value) == "":
                continue
            try:
                used.add(int(value))
            except (TypeError, ValueError):
                continue

        changed = False
        next_index = 1
        for event in events:
            if event_identifier(event) is not None:
                continue
            while next_index in used:
                next_index += 1
            event["index"] = next_index
            used.add(next_index)
            next_index += 1
            changed = True
        return changed

    def ensure_event_indices(self) -> bool:
        """Migrate only missing event identifiers and preserve file shape."""
        events, wrapped = self.load_events()
        if not self._assign_missing_event_indices(events):
            return False
        existing = read_json(self.events_path, {})
        if wrapped and isinstance(existing, dict):
            payload: Any = dict(existing)
            payload["events"] = events
        else:
            payload = events
        atomic_write_json(self.events_path, payload, backup=True)
        return True

    def update_event_status(
        self,
        event_name: str,
        status: str,
        *,
        event_id: Any = None,
        event_index: Any = None,
        **extra: Any,
    ) -> bool:
        """Update an event without changing the legacy list/object shape."""

        events, wrapped = self.load_events()
        if event_id is not None:
            matching = [
                event for event in events
                if str(event_identifier(event)) == str(event_id)
            ]
        elif event_index is not None:
            matching = [
                event for event in events
                if str(event_identifier(event)) == str(event_index)
            ]
        else:
            matching = [
                event for event in events
                if str(event.get("name", event.get("event_name", ""))) == str(event_name)
            ]
        # Long novels often reuse event titles such as "日常" or "先生的秘密".
        # Prefer the first unfinished occurrence so status updates do not keep
        # marking an earlier duplicate.
        ordered = [
            event for event in matching
            if event.get("status", "pending") not in {"completed", "failed"}
        ] + matching
        for event in ordered:
            event["status"] = status
            event["updated_at"] = now_iso()
            event.update(extra)
            self.save_events(events, wrapped=wrapped)
            return True
        return False

    def summary(self) -> Dict[str, Any]:
        chapters = sorted(self.chapters_dir.glob("ch*.md")) if self.chapters_dir.exists() else []
        events, _ = self.load_events()
        completed_events = sum(1 for event in events if event.get("status") == "completed")
        manifest = self.load_manifest()
        notes = read_json(self.project_dir / "writing_notes.json", {})
        total_words = 0
        notes_has_totals = False
        if isinstance(notes, dict):
            try:
                noted_words = int(notes.get("total_words", 0) or 0)
            except (TypeError, ValueError):
                noted_words = 0
            noted_events = notes.get("events_completed", []) or []
            notes_has_totals = bool(noted_words or noted_events)
            total_words = noted_words
            completed_events = max(completed_events, len(noted_events))
        if not chapters and isinstance(manifest.get("progress"), dict):
            progress = manifest["progress"]
            try:
                total_words = max(total_words, int(progress.get("total_words", 0) or 0))
                completed_events = max(
                    completed_events,
                    int(progress.get("completed_events", 0) or 0),
                )
            except (TypeError, ValueError):
                pass
        # A mature project already maintains totals in writing_notes.json.  Do
        # not reread every chapter on every status-bar refresh; imported or
        # legacy projects without totals still get a one-time filesystem scan.
        if not notes_has_totals:
            for chapter in chapters:
                try:
                    total_words += count_story_chars(chapter.read_text(encoding="utf-8"))
                except OSError:
                    continue
        return {
            "name": manifest.get("name", self.project_dir.name),
            "chapters": len(chapters),
            "events": completed_events,
            "words": total_words,
            "checkpoint": self.load_checkpoint(),
            "updated_at": manifest.get("updated_at", ""),
        }

    def is_safe_project_directory(self, root: Path | str) -> bool:
        """Return whether this project is a direct child of ``root``."""

        try:
            return self.project_dir.resolve().parent == Path(root).resolve()
        except OSError:
            return False


def create_project(projects_dir: Path | str, name: str, *, premise: str = "") -> ProjectStore:
    """Create a project directory with metadata, without overwriting one."""

    clean_name = validate_project_name(name)
    root = Path(projects_dir)
    target = root / clean_name
    if target.exists():
        raise FileExistsError(f"项目已存在：{clean_name}")
    store = ProjectStore(target)
    store.ensure_structure(name=clean_name, premise=premise)
    return store
