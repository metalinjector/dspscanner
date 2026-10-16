"""OCR settings must survive JSON -> CLI -> environment -> spawn."""
import pytest

from app.cli import _apply_runtime_application_settings, _parser
from app.settings_store import AppSettings, _apply_environment_overrides


@pytest.mark.parametrize("value", [False, "false", "False", "0", "off", "no"])
def test_russian_only_false_is_not_truthy_string(value):
    assert AppSettings(russian_only=value).sanitized().russian_only is False


@pytest.mark.parametrize("value", [True, "true", "1", "on", "yes"])
def test_russian_only_true(value):
    assert AppSettings(russian_only=value).sanitized().russian_only is True


@pytest.mark.parametrize("argv,tier,russian", [
    ([], "best", False),
    (["--ocr-model-tier", "fast", "--russian-only"], "fast", True),
    (["--ocr-model-tier", "medium", "--no-russian-only"], "medium", False),
])
def test_cli_model_and_language_config_propagates_to_environment(monkeypatch, argv, tier, russian):
    # CLI writes environment intentionally, restore it after the test.
    monkeypatch.setenv("DSP_SCANNER_OCR_MODEL_TIER", "fast")
    monkeypatch.setenv("DSP_SCANNER_RUSSIAN_ONLY", "true")
    args = _parser().parse_args(argv)
    _apply_runtime_application_settings(args, {"application": {"ocr_model_tier": "best", "russian_only": False}})
    settings = _apply_environment_overrides(AppSettings())
    assert settings.ocr_model_tier == tier
    assert settings.russian_only is russian


def test_cli_preserves_env_without_config_or_flags(monkeypatch):
    monkeypatch.setenv("DSP_SCANNER_OCR_MODEL_TIER", "medium")
    monkeypatch.setenv("DSP_SCANNER_RUSSIAN_ONLY", "0")
    _apply_runtime_application_settings(_parser().parse_args([]), {})
    settings = _apply_environment_overrides(AppSettings())
    assert settings.ocr_model_tier == "medium"
    assert settings.russian_only is False


@pytest.mark.parametrize("russian_only", [True, False])
def test_russian_only_setting_persists_without_resetting_user_choice(tmp_path, monkeypatch, russian_only):
    from app import settings_store
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.delenv("DSP_SCANNER_RUSSIAN_ONLY", raising=False)
    assert settings_store.save_settings(AppSettings(russian_only=russian_only))
    assert settings_store.load_settings().russian_only is russian_only
