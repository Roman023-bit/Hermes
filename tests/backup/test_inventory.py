import json

from hermes_backup.inventory import (
    classify,
    excluded_by,
    write_exclusions,
    write_inventory,
)


def test_dot_prefixed_caches_are_excluded():
    """`du /srv/hermes/data/*` hid these: .npm and .cache held 150 MB."""
    assert excluded_by(".npm/_npx/abc/node_modules/x.js")
    assert excluded_by(".cache/uv/wheels-v6/pypi/tabulate/meta.json")
    assert excluded_by(".local/share/pki/nssdb/key4.db") is None
    assert excluded_by(".env") is None


def test_recoverable_paths_are_excluded():
    assert excluded_by("cache/model.bin")
    assert excluded_by("cron/output/run-1.log")
    assert excluded_by("sessions/request_dump_20260715_231648.json")
    assert excluded_by("state.db-wal")
    assert excluded_by("kanban.db-shm")
    assert excluded_by(".DS_Store")
    assert excluded_by("sessions/sessions.json") is None


def test_known_paths_are_essential_and_new_ones_unclassified():
    assert classify("sessions/sessions.json") == "essential"
    assert classify("auth.json") == "essential"
    assert classify("cron/jobs.json") == "essential"
    assert classify("brand_new_thing.dat") == "unclassified"


def test_inventory_lists_staging_contents_with_checksums(tmp_path):
    staging = tmp_path / "staging"
    (staging / "sessions").mkdir(parents=True)
    (staging / "sessions" / "sessions.json").write_text("{}")
    (staging / "surprise.bin").write_bytes(b"1234")
    out = tmp_path / "INVENTORY.jsonl"

    totals = write_inventory(staging, out)

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    by_path = {row["path"]: row for row in rows}
    assert by_path["sessions/sessions.json"]["classification"] == "essential"
    assert by_path["surprise.bin"]["classification"] == "unclassified"
    assert by_path["surprise.bin"]["type"] == "file"
    assert len(by_path["surprise.bin"]["sha256"]) == 64
    assert totals.files == 2
    assert totals.total_bytes == 6
    assert totals.unclassified == 1


def test_symlink_is_recorded_by_target_not_by_content(tmp_path):
    staging = tmp_path / "staging"
    (staging / "skills").mkdir(parents=True)
    (staging / "skills" / "real.md").write_text("body")
    (staging / "skills" / "alias.md").symlink_to("real.md")
    out = tmp_path / "INVENTORY.jsonl"

    totals = write_inventory(staging, out)

    rows = {
        json.loads(line)["path"]: json.loads(line)
        for line in out.read_text().splitlines()
    }
    alias = rows["skills/alias.md"]
    assert alias["type"] == "symlink"
    assert alias["target"] == "real.md"
    assert "sha256" not in alias
    assert alias["classification"] == "essential"
    # The link contributes an entry but no bytes: only "body" is counted.
    assert totals.files == 2
    assert totals.total_bytes == 4


def test_broken_symlink_is_recorded_and_never_followed(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "dangling").symlink_to("nowhere/at/all")
    out = tmp_path / "INVENTORY.jsonl"

    totals = write_inventory(staging, out)

    row = json.loads(out.read_text().splitlines()[0])
    assert row == {
        "path": "dangling",
        "type": "symlink",
        "target": "nowhere/at/all",
        "classification": "unclassified",
    }
    assert totals.files == 1


def test_symlinked_directory_is_recorded_but_not_descended(tmp_path):
    outside = tmp_path / "outside"
    (outside / "secret").mkdir(parents=True)
    (outside / "secret" / "leak.txt").write_text("should never be inventoried")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "escape").symlink_to(outside, target_is_directory=True)
    out = tmp_path / "INVENTORY.jsonl"

    totals = write_inventory(staging, out)

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [row["path"] for row in rows] == ["escape"]
    assert rows[0]["type"] == "symlink"
    assert totals.files == 1


def test_excluded_symlink_lands_in_exclusions_with_its_target(tmp_path):
    source = tmp_path / "data"
    (source / ".npm" / "_npx" / "bin").mkdir(parents=True)
    (source / ".npm" / "_npx" / "bin" / "mcp-proxy").symlink_to("../lib/proxy.js")
    out = tmp_path / "EXCLUSIONS.jsonl"

    count = write_exclusions(source, out)

    row = json.loads(out.read_text().splitlines()[0])
    assert count == 1
    assert row["type"] == "symlink"
    assert row["target"] == "../lib/proxy.js"
    assert row["rule"] == ".npm/*"
    assert "size" not in row


def test_paths_with_tabs_and_newlines_survive_round_trip(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    hostile = staging / "weird\tname\nfile"
    hostile.write_text("x")
    out = tmp_path / "INVENTORY.jsonl"

    write_inventory(staging, out)

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert rows[0]["path"] == "weird\tname\nfile"


def test_exclusions_are_taken_from_the_source_tree(tmp_path):
    source = tmp_path / "data"
    (source / "cache").mkdir(parents=True)
    (source / "cache" / "big.bin").write_bytes(b"0" * 10)
    (source / "sessions").mkdir()
    (source / "sessions" / "sessions.json").write_text("{}")
    out = tmp_path / "EXCLUSIONS.jsonl"

    count = write_exclusions(source, out)

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert count == 1
    assert rows[0]["path"] == "cache/big.bin"
    assert rows[0]["classification"] == "excluded-recoverable"
    assert rows[0]["size"] == 10
    assert "sha256" not in rows[0]


def test_inventory_and_exclusions_do_not_overlap(tmp_path):
    source = tmp_path / "data"
    (source / "cache").mkdir(parents=True)
    (source / "cache" / "big.bin").write_bytes(b"0")
    (source / "auth.json").write_text("{}")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "auth.json").write_text("{}")

    write_exclusions(source, tmp_path / "EXCLUSIONS.jsonl")
    write_inventory(staging, tmp_path / "INVENTORY.jsonl")

    excluded = {
        json.loads(line)["path"]
        for line in (tmp_path / "EXCLUSIONS.jsonl").read_text().splitlines()
    }
    included = {
        json.loads(line)["path"]
        for line in (tmp_path / "INVENTORY.jsonl").read_text().splitlines()
    }
    assert excluded & included == set()
