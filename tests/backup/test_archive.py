import os
import tarfile

import pytest

from hermes_backup.archive import ArchiveError, create, extract, validate


def _archive_with(tmp_path, mutate):
    target = tmp_path / "evil.tar.gz"
    with tarfile.open(target, "w:gz") as tar:
        mutate(tar)
    return target


def test_round_trip_preserves_content_and_modes(tmp_path):
    staging = tmp_path / "staging"
    (staging / "cron").mkdir(parents=True)
    (staging / "cron" / "jobs.json").write_text('{"jobs": []}')
    secret = staging / "auth.json"
    secret.write_text("{}")
    secret.chmod(0o600)
    archive = tmp_path / "essential.tar.gz"

    create(staging, archive)
    restored = tmp_path / "restored"
    extract(archive, restored)

    assert (restored / "cron" / "jobs.json").read_text() == '{"jobs": []}'
    assert (restored / "auth.json").stat().st_mode & 0o777 == 0o600


def test_internal_symlink_survives_the_round_trip(tmp_path):
    """Links inside the tree are legitimate state and must be preserved.

    Task 5 records them in INVENTORY.jsonl without dereferencing; the
    archive has to carry them as links, not as copies of their target.
    """
    staging = tmp_path / "staging"
    (staging / "skills").mkdir(parents=True)
    (staging / "skills" / "real.md").write_text("body")
    (staging / "skills" / "alias.md").symlink_to("real.md")
    archive = tmp_path / "essential.tar.gz"

    create(staging, archive)
    validate(archive)
    restored = tmp_path / "restored"
    extract(archive, restored)

    alias = restored / "skills" / "alias.md"
    assert alias.is_symlink()
    assert os.readlink(alias) == "real.md"


def test_absolute_path_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("/etc/passwd")
        info.size = 0
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="absolute"):
        validate(_archive_with(tmp_path, mutate))


def test_parent_traversal_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("../escape")
        info.size = 0
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="traversal"):
        validate(_archive_with(tmp_path, mutate))


def test_symlink_outside_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../etc/passwd"
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="link"):
        validate(_archive_with(tmp_path, mutate))


def test_hardlink_outside_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("hard")
        info.type = tarfile.LNKTYPE
        info.linkname = "../outside"
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="link"):
        validate(_archive_with(tmp_path, mutate))


def test_device_node_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("dev/null")
        info.type = tarfile.CHRTYPE
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="special"):
        validate(_archive_with(tmp_path, mutate))


def test_fifo_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("pipe")
        info.type = tarfile.FIFOTYPE
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="special"):
        validate(_archive_with(tmp_path, mutate))


def test_extract_validates_before_writing_anything(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("../escape")
        info.size = 0
        tar.addfile(info)

    archive = _archive_with(tmp_path, mutate)
    destination = tmp_path / "restored"
    with pytest.raises(ArchiveError):
        extract(archive, destination)
    assert not destination.exists() or not os.listdir(destination)
