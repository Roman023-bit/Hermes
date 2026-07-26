import hashlib

from hermes_backup.hashing import atomic_write_text, sha256_file, write_sha256sums


def test_sha256_matches_hashlib(tmp_path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"hermes" * 1000)
    assert sha256_file(target) == hashlib.sha256(b"hermes" * 1000).hexdigest()


def test_atomic_write_leaves_no_partial_file(tmp_path):
    target = tmp_path / "STATE"
    atomic_write_text(target, "SOURCE_HOST=aeza\n")
    assert target.read_text() == "SOURCE_HOST=aeza\n"
    # Only files: the repo-wide autouse fixture in tests/conftest.py puts a
    # hermes_test/ directory into every tmp_path.
    assert [item.name for item in tmp_path.iterdir() if item.is_file()] == ["STATE"]
    assert target.stat().st_mode & 0o777 == 0o600


def test_sha256sums_covers_payload_but_not_itself(tmp_path):
    (tmp_path / "essential.tar.gz").write_bytes(b"archive")
    (tmp_path / "STATE").write_text("x\n")
    sums = write_sha256sums(tmp_path)
    listed = {line.split("  ", 1)[1] for line in sums.read_text().splitlines()}
    assert listed == {"essential.tar.gz", "STATE"}
    assert "SHA256SUMS" not in listed
