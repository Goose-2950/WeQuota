from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _names(path: Path) -> set[str]:
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.add(line.split("==", 1)[0].split(">=", 1)[0].split("[", 1)[0].lower())
    return names


def test_runtime_requirements_include_voice_and_model_download_dependencies():
    names = _names(ROOT / "requirements.txt")
    assert {"faster-whisper", "av", "huggingface-hub"} <= names
    assert "pytest" not in names


def test_development_requirements_are_separate():
    names = _names(ROOT / "requirements-dev.txt")
    assert {"pytest", "httpx"} <= names
    assert "pytest" not in _names(ROOT / "requirements.txt")


def test_runtime_lock_is_exact_and_excludes_test_tools():
    names = _names(ROOT / "requirements.lock")
    assert {"faster-whisper", "av", "huggingface-hub"} <= names
    assert "pytest" not in names
    assert "httpx" not in names
    assert all("==" in line for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.lstrip().startswith("#"))
