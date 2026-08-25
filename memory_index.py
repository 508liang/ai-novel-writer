"""Lightweight persistent chapter memory for long-running novel projects.

This is intentionally dependency-free.  It is not a replacement for a vector
database; it gives the current desktop application a durable index, recent
chapter context, and useful lexical retrieval before a heavier semantic layer
is introduced.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from project_store import atomic_write_json, count_story_chars, now_iso


INDEX_VERSION = 1


def _chapter_number(path: Path) -> int:
    match = re.match(r"ch(\d+)", path.name, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _event_name(path: Path) -> str:
    stem = path.stem
    return stem.split("_", 1)[1] if "_" in stem else stem


def _terms(value: str) -> List[str]:
    """Extract searchable words and Chinese character bigrams."""

    text = (value or "").lower().strip()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text)
    compact = "".join(words)
    bigrams = [compact[i:i + 2] for i in range(max(0, len(compact) - 1))]
    return list(dict.fromkeys(words + bigrams))


class StoryMemory:
    """Persistent chapter metadata and lexical context retrieval."""

    def __init__(self, project_dir: Path | str):
        self.project_dir = Path(project_dir)
        self.memory_dir = self.project_dir / "memory"
        self.index_path = self.memory_dir / "chapter_index.json"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Any] = {"version": INDEX_VERSION, "chapters": []}
        self._loaded = False

    def _load(self) -> Dict[str, Any]:
        if self._loaded:
            return self._data
        self._loaded = True
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("chapters"), list):
                    self._data = data
            except (OSError, json.JSONDecodeError):
                self._data = {"version": INDEX_VERSION, "chapters": []}
        return self._data

    def _save(self) -> None:
        self._data["version"] = INDEX_VERSION
        self._data["updated_at"] = now_iso()
        atomic_write_json(self.index_path, self._data, backup=True)

    def records(self) -> List[Dict[str, Any]]:
        return list(self._load().get("chapters", []))

    def rebuild_if_needed(self) -> bool:
        """Index new/changed chapters and return whether the index changed."""

        chapters_dir = self.project_dir / "chapters"
        chapters = sorted(chapters_dir.glob("ch*.md")) if chapters_dir.exists() else []
        old_by_path = {item.get("path"): item for item in self.records() if isinstance(item, dict)}
        new_records: List[Dict[str, Any]] = []
        changed = set(old_by_path) != {path.name for path in chapters}

        for path in chapters:
            stat = path.stat()
            signature = f"{stat.st_size}:{stat.st_mtime_ns}"
            old = old_by_path.get(path.name)
            if old and old.get("signature") == signature:
                new_records.append(old)
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            new_records.append({
                "path": path.name,
                "number": _chapter_number(path),
                "event_name": _event_name(path),
                "summary": self._fallback_summary(text),
                "keywords": _terms(text)[:240],
                "word_count": count_story_chars(text),
                "signature": signature,
                "updated_at": now_iso(),
            })
            changed = True

        new_records.sort(key=lambda item: (item.get("number", 0), item.get("path", "")))
        if changed or not self.index_path.exists():
            self._load()
            self._data["chapters"] = new_records
            self._save()
        return changed

    @staticmethod
    def _fallback_summary(text: str, limit: int = 240) -> str:
        compact = re.sub(r"\s+", " ", text or "").strip()
        return compact[:limit]

    def upsert_chapter(
        self,
        chapter_path: Path | str,
        *,
        event_name: str = "",
        summary: str = "",
        volume: Optional[int] = None,
        text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert or update one chapter after the controller commits it."""

        path = Path(chapter_path)
        if text is None:
            text = path.read_text(encoding="utf-8")
        stat = path.stat()
        record: Dict[str, Any] = {
            "path": path.name,
            "number": _chapter_number(path),
            "event_name": event_name or _event_name(path),
            "summary": summary or self._fallback_summary(text),
            "keywords": _terms(text)[:240],
            "word_count": count_story_chars(text),
            "signature": f"{stat.st_size}:{stat.st_mtime_ns}",
            "updated_at": now_iso(),
        }
        if volume is not None:
            record["volume"] = volume
        records = [item for item in self.records() if item.get("path") != path.name]
        records.append(record)
        records.sort(key=lambda item: (item.get("number", 0), item.get("path", "")))
        self._data["chapters"] = records
        self._save()
        return record

    def recent(self, limit: int = 3) -> List[Dict[str, Any]]:
        return self.records()[-max(0, limit):]

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        query_terms = _terms(query)
        if not query_terms:
            return []
        scored = []
        for record in self.records():
            haystack = " ".join(
                str(record.get(key, ""))
                for key in ("event_name", "summary", "path", "keywords")
            ).lower()
            score = sum(haystack.count(term) for term in query_terms)
            if score:
                scored.append((score, record.get("number", 0), record))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[:max(0, limit)]]

    def build_context(self, query: str = "", *, recent: int = 3, max_chars: int = 5000) -> str:
        """Build bounded context with recent chapters plus relevant index hits."""

        self.rebuild_if_needed()
        chosen: List[Dict[str, Any]] = []
        seen = set()
        for record in self.recent(recent) + self.search(query, limit=5):
            path = record.get("path")
            if path and path not in seen:
                chosen.append(record)
                seen.add(path)

        parts: List[str] = []
        used = 0
        for record in reversed(chosen):
            path = self.project_dir / "chapters" / str(record.get("path", ""))
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = str(record.get("summary", ""))
            excerpt = text[:500] + ("\n…\n" + text[-500:] if len(text) > 1100 else "")
            block = f"【{record.get('path', '')}｜{record.get('event_name', '')}】\n{excerpt}"
            if used + len(block) > max_chars:
                remaining = max_chars - used
                if remaining > 120:
                    parts.append(block[:remaining])
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)
