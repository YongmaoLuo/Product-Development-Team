import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from task import UNKNOWN_MODIFICATIONS_SENTINEL, SubTask


LEGACY_TASKS_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "legacy_tasks.json"
)


@pytest.mark.files_to_modify
class TestSubTaskFilesToModify:
    """Parameterized validation tests for SubTask.files_to_modify."""

    @pytest.mark.parametrize(
        "path",
        [
            "src/a.py",
            "b/c/d.ts",
            "file.py",
            "dir/nested/file.go",
        ],
    )
    def test_validates_relative_path(self, path: str) -> None:
        json_str = json.dumps(
            {
                "id": "1",
                "title": "x",
                "description": "d",
                "files_to_modify": [path],
            }
        )
        task = SubTask.model_validate_json(json_str)
        assert task.files_to_modify == [path]

    def test_defaults_to_sentinel(self) -> None:
        json_str = json.dumps({"id": "1", "title": "x", "description": "d"})
        task = SubTask.model_validate_json(json_str)
        assert task.files_to_modify == UNKNOWN_MODIFICATIONS_SENTINEL

    def test_empty_list_preserved_through_subtask(self) -> None:
        """Empty ``files_to_modify=[]`` is preserved verbatim by SubTask
        (the read-only tasks needed ``[]`` for parallel-scheduling, not
        sentinel — see
        ``task.py:118-132``). The dispatcher's post-read gate
        (``framework/task_output_validator.py`` step-3) rejects this,
        so callers MUST route ``[]`` through ``TasksGenerator.
        _backfill_files_to_modify_from_description`` before loading.
        """
        json_str = json.dumps(
            {"id": "1", "title": "x", "description": "d", "files_to_modify": []}
        )
        task = SubTask.model_validate_json(json_str)
        assert task.files_to_modify == []

    @pytest.mark.parametrize(
        "path",
        [
            "/abs/a.py",
        ],
    )
    def test_rejects_absolute(self, path) -> None:
        json_str = json.dumps(
            {
                "id": "1",
                "title": "x",
                "description": "d",
                "files_to_modify": [path],
            }
        )
        with pytest.raises(ValidationError):
            SubTask.model_validate_json(json_str)

    @pytest.mark.parametrize(
        "path",
        [
            "../x.py",
            "a/../b",
        ],
    )
    def test_rejects_parent_traversal(self, path) -> None:
        json_str = json.dumps(
            {
                "id": "1",
                "title": "x",
                "description": "d",
                "files_to_modify": [path],
            }
        )
        with pytest.raises(ValidationError):
            SubTask.model_validate_json(json_str)

    def test_invalid_empty_path(self) -> None:
        json_str = json.dumps(
            {
                "id": "1",
                "title": "x",
                "description": "d",
                "files_to_modify": [""],
            }
        )
        with pytest.raises(ValidationError):
            SubTask.model_validate_json(json_str)

    def test_json_roundtrip(self) -> None:
        original = SubTask(
            id="1", title="x", description="d", files_to_modify=["src/a.py"]
        )
        dumped = original.model_dump_json()
        loaded = SubTask.model_validate_json(dumped)
        assert loaded.files_to_modify == ["src/a.py"]

    @pytest.mark.parametrize("element", [123, None, {}])
    def test_non_string_element_raises(self, element) -> None:
        json_str = json.dumps(
            {
                "id": "1",
                "title": "x",
                "description": "d",
                "files_to_modify": ["src/a.py", element],
            }
        )
        with pytest.raises(ValidationError):
            SubTask.model_validate_json(json_str)
@pytest.mark.files_to_modify
class TestBackwardCompatibility:
    """Legacy tasks.json files (pre-files_to_modify) must load without error.

    Older plans produced by the autonomous-coding system predate the
    ``files_to_modify`` field on SubTask. The loader must continue to
    accept those files and resolve the missing metadata to the
    ``UNKNOWN_MODIFICATIONS_SENTINEL`` list so downstream layer
    builders and file-lock consumers see a well-defined value.
    """

    def test_legacy_tasks_fixture_loads(self) -> None:
        """The fixture file must exist and parse as valid JSON."""
        assert LEGACY_TASKS_FIXTURE.exists(), (
            f"Missing legacy fixture at {LEGACY_TASKS_FIXTURE}"
        )
        with LEGACY_TASKS_FIXTURE.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        assert "tasks" in payload and isinstance(payload["tasks"], list)
        assert len(payload["tasks"]) >= 2

    def test_legacy_tasks_parse_without_files_to_modify(self) -> None:
        """Each legacy SubTask must parse without raising and default files_to_modify to sentinel."""
        with LEGACY_TASKS_FIXTURE.open("r", encoding="utf-8") as f:
            payload = json.load(f)

        parsed: list[SubTask] = []
        for entry in payload["tasks"]:
            # The legacy format omits files_to_modify; this must not raise.
            parsed.append(SubTask.model_validate(entry))

        assert parsed, "fixture must contain at least one task"
        for task in parsed:
            assert "files_to_modify" not in task.model_fields_set, (
                f"Task {task.id!r} should not have files_to_modify explicitly set in legacy fixture"
            )
            assert task.files_to_modify == UNKNOWN_MODIFICATIONS_SENTINEL, (
                f"Task {task.id!r} should default to sentinel, "
                f"got {task.files_to_modify!r}"
            )

    def test_legacy_json_string_roundtrip(self) -> None:
        """A bare task JSON without files_to_modify must round-trip via model_validate_json."""
        legacy_json = json.dumps(
            {
                "id": "legacy-1",
                "title": "Legacy task",
                "description": "No files_to_modify field present.",
                "test_command": "echo legacy",
            }
        )
        task = SubTask.model_validate_json(legacy_json)
        assert task.files_to_modify == UNKNOWN_MODIFICATIONS_SENTINEL
        assert task.id == "legacy-1"
