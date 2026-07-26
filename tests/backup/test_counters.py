import json

import pytest

from hermes_backup.counters import (
    CounterError,
    count_cron_jobs,
    count_plugins,
    count_sessions,
    count_skills,
)


def test_skill_is_a_directory_with_skill_md_at_any_depth(tmp_path):
    (tmp_path / "apple" / "apple-macos-automation").mkdir(parents=True)
    (tmp_path / "apple" / "apple-macos-automation" / "SKILL.md").write_text("x")
    (tmp_path / "apple" / "bundle" / "nested").mkdir(parents=True)
    (tmp_path / "apple" / "bundle" / "nested" / "SKILL.md").write_text("x")
    (tmp_path / "apple" / "notes").mkdir()
    (tmp_path / "apple" / "notes" / "DESCRIPTION.md").write_text("x")
    # Категория верхнего уровня сама навыком не является.
    assert count_skills(tmp_path) == 2


def test_plugin_is_a_directory_with_plugin_yaml_at_any_depth(tmp_path):
    (tmp_path / "restrict-guest-tools").mkdir()
    (tmp_path / "restrict-guest-tools" / "plugin.yaml").write_text("x")
    (tmp_path / "image_gen" / "replicate").mkdir(parents=True)
    (tmp_path / "image_gen" / "replicate" / "plugin.yaml").write_text("x")
    assert count_plugins(tmp_path) == 2


def test_sessions_are_top_level_keys_of_sessions_json(tmp_path):
    target = tmp_path / "sessions.json"
    target.write_text(
        json.dumps({"agent:main:telegram:dm:1": {}, "agent:main:telegram:dm:2": {}})
    )
    assert count_sessions(target) == 2


def test_cron_jobs_come_from_the_jobs_list(tmp_path):
    target = tmp_path / "jobs.json"
    target.write_text(json.dumps({"jobs": [{"id": "a"}, {"id": "b"}]}))
    assert count_cron_jobs(target) == 2


def test_empty_job_list_is_valid(tmp_path):
    target = tmp_path / "jobs.json"
    target.write_text(json.dumps({"jobs": []}))
    assert count_cron_jobs(target) == 0


def test_wrong_cron_schema_is_rejected(tmp_path):
    target = tmp_path / "jobs.json"
    target.write_text(json.dumps(["a", "b"]))
    with pytest.raises(CounterError, match="jobs"):
        count_cron_jobs(target)


def test_truncated_json_is_rejected(tmp_path):
    target = tmp_path / "jobs.json"
    target.write_text('{"jobs": [{"id": "a"')
    with pytest.raises(CounterError):
        count_cron_jobs(target)
