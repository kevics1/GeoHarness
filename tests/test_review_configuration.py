"""Independent image settings resolve from the same user file as the runtime."""

import json
from dataclasses import replace

from typer.testing import CliRunner

from geoharness.cartography.review_config import review_configuration
from geoharness.cli import _generate_config_template, app
from geoharness.config.settings import (
    AgentConfig,
    GeoConfig,
    MapReviewConfig,
    load_geo_config,
    resolve_config_path,
)


def test_default_user_yaml_controls_review_without_changing_main(tmp_path, monkeypatch):
    monkeypatch.delenv("GEOH_CONFIG_PATH", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    root = tmp_path / ".geoharness"
    root.mkdir()
    (root / ".env").write_text("TEST_REVIEW_KEY=private-image-key\n", encoding="utf-8")
    path = root / "config.yaml"
    path.write_text(
        "model: main-model\napi_key: main-key\nbase_url: https://main.invalid/v1\n"
        "agent:\n  reasoning_effort: max\nmap_review:\n  model: image-model\n"
        "  api_key: ${TEST_REVIEW_KEY}\n  base_url: https://image.invalid/v1\n"
        "  reasoning_effort: max\n  response_format: json\n", encoding="utf-8")
    config = load_geo_config()
    assert resolve_config_path() == path
    assert (config.model, config.api_key, config.agent.reasoning_effort) == (
        "main-model", "main-key", "max")
    assert config.map_review.api_key == "private-image-key"
    info = review_configuration(config)
    assert info["model"] == "image-model"
    assert info["model_source"] == info["endpoint_source"] == info["credential_source"] == (
        "map_review")
    assert info["response_format"] == "json"
    assert not info["configuration_errors"]
    assert not any(secret in json.dumps(info) for secret in (
        "private-image-key", "main-key", "image.invalid", "main.invalid"))
    path.write_text(path.read_text().replace("image-model", "second-image"), encoding="utf-8")
    assert load_geo_config().map_review.model == "second-image"


def test_fallback_is_per_field_and_reasoning_is_not_inherited():
    config = GeoConfig(model="main", api_key="key", base_url="https://main.invalid/v1",
        agent=AgentConfig(reasoning_effort="max"), map_review=MapReviewConfig(model="image"))
    info = review_configuration(config)
    assert info["model_source"] == "map_review"
    assert info["endpoint_source"] == info["credential_source"] == "main"
    assert info["reasoning_effort"] is None
    assert info["reasoning_source"] == "provider_default"
    config = replace(config, map_review=MapReviewConfig(request_extra_body={
        "thinking": {"type": "disabled"}}))
    assert review_configuration(config)["reasoning_source"] == "request_extra_body"


def test_explicit_config_precedes_env_path(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOH_CONFIG_PATH", str(tmp_path / "env.yaml"))
    assert resolve_config_path() == tmp_path / "env.yaml"
    assert resolve_config_path(tmp_path / "explicit.yaml") == tmp_path / "explicit.yaml"


def test_review_doctor_default_is_offline_and_secret_free(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("model: main\napi_key: hidden-secret\nmap_review:\n  model: vision\n",
                    encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline inspection sent an image")
    monkeypatch.setattr("geoharness.cartography.review_doctor.run_review_probe", forbidden)
    result = CliRunner().invoke(app, ["review-doctor", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert "hidden-secret" not in result.output
    assert json.loads(result.output)["model"] == "vision"
    assert not (tmp_path / ".geoharness").exists()


def test_review_doctor_requires_explicit_live_and_reports_unresolved_config(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["review-doctor", "--image", "map.png"])
    assert result.exit_code != 0
    path = tmp_path / "config.yaml"
    path.write_text("model: ${UNRESOLVED_REVIEW_MODEL}\napi_key: ''\n", encoding="utf-8")
    result = runner.invoke(app, ["review-doctor", "--config", str(path)])
    assert result.exit_code == 1
    assert "review_model_missing_or_unresolved" in result.output


def test_init_template_exposes_independent_review_fields():
    import yaml

    review = yaml.safe_load(_generate_config_template())["map_review"]
    assert {"model", "base_url", "api_key", "reasoning_effort", "response_format",
            "request_extra_body", "timeout_seconds", "read_timeout_seconds"} <= review.keys()


def test_review_option_case_normalization_preserves_model_identifier():
    settings = MapReviewConfig(model="Provider/Vision(Medium)", reasoning_effort=" Medium ",
                               response_format="JSON")
    assert settings.reasoning_effort == "medium"
    assert settings.response_format == "json"
    assert settings.model == "Provider/Vision(Medium)"


def test_doctor_validation_names_field_without_echoing_private_input(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("map_review:\n  response_format: secret-mistyped-option\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["review-doctor", "--config", str(path)])
    assert result.exit_code == 1
    assert '"field": "response_format"' in result.output
    assert "secret-mistyped-option" not in result.output
