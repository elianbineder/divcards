"""The `costs` commands, run end to end on the synthetic dataset."""

import json

from poe_divcards.cli import main


def test_costs_template_import_and_check(dataset_dir, tmp_path, capsys):
    tsv = tmp_path / "league.tsv"
    out = tmp_path / "league.json"
    assert main(["costs", "template", str(dataset_dir), "-o", str(tsv), "--league", "Test"]) == 0
    raw = tsv.read_bytes()
    assert b"\r\n" not in raw                      # LF on every platform
    text = raw.decode("utf-8")
    assert "\tThe Doctor" in text and "The Cartographer" not in text   # disabled cards left out

    tsv.write_text(text.replace("\tThe Doctor", "670\tThe Doctor"), encoding="utf-8", newline="\n")
    assert main(["costs", "import", str(dataset_dir), str(tsv), "-o", str(out), "--league", "Test",
                 "--updated-at", "2026-10-06"]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["costs"]["the-doctor"] == 675     # rounded to its step
    assert data["updated_at"] == "2026-10-06"
    assert b"\r\n" not in out.read_bytes()
    assert "The Doctor: 670 -> 675" in capsys.readouterr().out

    assert main(["costs", "check", str(dataset_dir), str(out)]) == 0
    assert "0 problems" in capsys.readouterr().out


def test_costs_files_are_not_overwritten_without_force(dataset_dir, tmp_path):
    tsv = tmp_path / "league.tsv"
    tsv.write_text("keep me", encoding="utf-8")
    assert main(["costs", "template", str(dataset_dir), "-o", str(tsv)]) == 1
    assert tsv.read_text(encoding="utf-8") == "keep me"
