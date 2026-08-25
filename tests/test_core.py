"""Regression tests for project storage, memory and checkpoint recovery."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memory_index import StoryMemory
from project_store import (
    ProjectStore,
    atomic_write_json,
    atomic_write_text,
    chapter_filename,
    create_project,
    event_identifier,
    safe_filename,
    validate_project_name,
)


class ProjectCoreTests(unittest.TestCase):
    def temp_path(self, name: str = "project") -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name) / name

    def test_project_creation_manifest_and_name_validation(self):
        root = self.temp_path("projects")
        store = create_project(root, "星海长夜", premise="一名观测员发现了会呼吸的星门")

        self.assertTrue(store.manifest_path.exists())
        self.assertEqual(store.load_manifest()["name"], "星海长夜")
        self.assertEqual(store.load_manifest()["premise"], "一名观测员发现了会呼吸的星门")
        for directory in ProjectStore.REQUIRED_DIRS:
            self.assertTrue((store.project_dir / directory).is_dir())
        with self.assertRaises(FileExistsError):
            create_project(root, "星海长夜")

        self.assertEqual(validate_project_name("  测试项目  "), "测试项目")
        for invalid in (
            "", ".", "..", "a/b", r"a\b", "a:b", "a?b", "坏\n项目",
            "CON", "项目.", "字" * 101,
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_project_name(invalid)

    def test_atomic_writes_keep_previous_backup(self):
        path = self.temp_path("data") / "state.json"
        atomic_write_text(path, "旧内容")
        atomic_write_text(path, "新内容", backup=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "新内容")
        self.assertEqual(path.with_name("state.json.bak").read_text(encoding="utf-8"), "旧内容")

        atomic_write_json(path, {"ok": True}, backup=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})
        self.assertIn("新内容", path.with_name("state.json.bak").read_text(encoding="utf-8"))

    def test_event_status_supports_list_object_id_index_and_duplicates(self):
        store = ProjectStore(self.temp_path())
        store.ensure_structure()
        events = [
            {"id": 0, "name": "日常", "status": "completed"},
            {"id": 2, "name": "日常", "status": "pending"},
        ]
        store.save_events(events)

        self.assertTrue(store.update_event_status("日常", "writing", event_index=2))
        loaded, wrapped = store.load_events()
        self.assertFalse(wrapped)
        self.assertEqual([event["status"] for event in loaded], ["completed", "writing"])

        wrapped_events = [
            {"index": 0, "name": "序章", "status": "pending"},
            {"index": 1, "name": "序章", "status": "pending"},
        ]
        atomic_write_json(
            store.events_path,
            {"volume": 3, "volume_name": "潮声", "events": wrapped_events},
        )
        store.save_events(wrapped_events, wrapped=True)
        self.assertTrue(store.update_event_status("序章", "completed", event_index=0))
        loaded, wrapped = store.load_events()
        self.assertTrue(wrapped)
        self.assertEqual([event["status"] for event in loaded], ["completed", "pending"])
        wrapped_payload = json.loads(store.events_path.read_text(encoding="utf-8"))
        self.assertEqual(wrapped_payload["volume"], 3)
        self.assertEqual(wrapped_payload["volume_name"], "潮声")

        legacy = ProjectStore(self.temp_path("legacy"))
        legacy.ensure_structure()
        atomic_write_json(legacy.events_path, [{"name": "无编号"}])
        self.assertTrue(legacy.ensure_event_indices())
        legacy_events, _ = legacy.load_events()
        self.assertEqual(legacy_events[0]["index"], 1)
        self.assertEqual(event_identifier({"index": None, "id": 4}), 4)

    def test_checkpoint_round_trip_and_cleanup(self):
        store = ProjectStore(self.temp_path())
        store.ensure_structure()
        saved = store.save_checkpoint(
            {"stage": "writing", "event_name": "试写", "event_index": 0},
            draft_text="这是一个可恢复的草稿。",
        )
        self.assertEqual(saved["stage"], "writing")
        self.assertEqual(store.load_checkpoint()["event_index"], 0)
        self.assertEqual(store.read_checkpoint_draft(), "这是一个可恢复的草稿。")

        store.save_checkpoint({"stage": "paused", "event_name": "试写"})
        self.assertTrue(store.checkpoint_path.with_name("checkpoint.json.bak").exists())
        store.clear_checkpoint()
        self.assertIsNone(store.load_checkpoint())
        self.assertEqual(store.read_checkpoint_draft(), "")

    def test_memory_rebuild_search_and_bounded_context(self):
        project = self.temp_path()
        chapters = project / "chapters"
        chapters.mkdir(parents=True)
        (chapters / "ch001_初见.md").write_text("主角在雨夜第一次看见远方的灯塔。", encoding="utf-8")
        (chapters / "ch002_潮汐.md").write_text("潮汐升起，灯塔下出现一封没有署名的信。", encoding="utf-8")

        memory = StoryMemory(project)
        self.assertTrue(memory.rebuild_if_needed())
        self.assertEqual(len(memory.records()), 2)
        self.assertEqual(memory.search("灯塔", limit=1)[0]["path"], "ch002_潮汐.md")
        context = memory.build_context("潮汐", recent=2, max_chars=180)
        self.assertLessEqual(len(context), 180)
        self.assertIn("潮汐", context)

        (chapters / "ch002_潮汐.md").write_text("潮汐退去，灯塔下只剩一枚发热的银币。", encoding="utf-8")
        self.assertTrue(memory.rebuild_if_needed())
        self.assertIn("银币", memory.records()[-1]["summary"])

    def test_summary_uses_persisted_totals_without_reading_all_chapters(self):
        project = self.temp_path()
        store = ProjectStore(project)
        store.ensure_structure()
        atomic_write_json(
            project / "writing_notes.json",
            {"total_words": 123456, "events_completed": [{"name": "已完成"}]},
        )
        chapters = project / "chapters"
        chapters.mkdir(exist_ok=True)
        (chapters / "ch001_统计.md").write_text("不会被状态刷新再次读取", encoding="utf-8")
        summary = store.summary()
        self.assertEqual(summary["words"], 123456)
        self.assertEqual(summary["events"], 1)

    def test_safe_filename_and_chapter_filename(self):
        safe = safe_filename('  雨/夜:*? "试读"  ')
        self.assertNotRegex(safe, r'[<>:"/\\|?*]')
        self.assertEqual(len(safe_filename("字" * 200)), 100)
        self.assertEqual(safe_filename("CON"), "_CON")
        self.assertEqual(chapter_filename(7, "序章/相遇"), "ch007_序章_相遇.md")

    def test_controller_initializes_isolated_project_without_api_call(self):
        from agents import NovelWritingSystem

        project = self.temp_path()
        system = NovelWritingSystem(project, stop_checker=lambda: False)
        self.assertEqual(system.project_dir, project)
        self.assertFalse(system.stop_checker())
        self.assertTrue((project / "config.json").exists())
        self.assertTrue((project / "project.json").exists())
        self.assertTrue((project / "memory" / "chapter_index.json").exists())

    def test_resume_committing_checkpoint_does_not_duplicate_chapter(self):
        from agents import NovelWritingSystem

        project = self.temp_path()
        chapters = project / "chapters"
        chapters.mkdir(parents=True)
        event_name = "断点事件"
        chapter_name = chapter_filename(1, event_name)
        draft = "这是已经写入磁盘、等待提交完成的章节。" * 20
        (chapters / chapter_name).write_text(draft, encoding="utf-8")

        system = NovelWritingSystem(project, stop_checker=lambda: False)
        system.editor.analyze_chapter = lambda text, ctx: {}
        system._update_tracking = lambda *args, **kwargs: None
        system.master_director.summarize_chapter = lambda text, name: "断点恢复摘要"
        system.store.save_events(
            [{"id": 0, "name": event_name, "summary": "已生成", "status": "writing"}]
        )
        system.store.save_checkpoint(
            {
                "stage": "committing",
                "status": "running",
                "event_name": event_name,
                "event_summary": "已生成",
                "event_index": 0,
                "chapter_file": chapter_name,
            },
            draft_text=draft,
        )

        result = system.resume_checkpoint()
        self.assertEqual(result, draft)
        self.assertEqual(sorted(path.name for path in chapters.glob("ch*.md")), [chapter_name])
        self.assertIsNone(system.store.load_checkpoint())
        events, _ = system.store.load_events()
        self.assertEqual(events[0]["status"], "completed")
        self.assertEqual(events[0]["chapter_files"], [chapter_name])


if __name__ == "__main__":
    unittest.main()
