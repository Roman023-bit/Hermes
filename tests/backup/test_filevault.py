import pytest

from hermes_backup.filevault import FileVaultOff, require_filevault


def test_gate_passes_when_filevault_is_active():
    require_filevault(command=["/usr/bin/true"])


def test_gate_fails_closed_when_filevault_is_off():
    with pytest.raises(FileVaultOff):
        require_filevault(command=["/usr/bin/false"])


def test_gate_fails_closed_when_the_tool_is_missing():
    with pytest.raises(FileVaultOff):
        require_filevault(command=["/nonexistent/fdesetup", "isactive"])
