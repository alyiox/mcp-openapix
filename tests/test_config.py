"""Tests for config.py loader + defaults validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_openapix.config import (
    Config,
    ConfigError,
    get_deployment,
    load_config,
    resolve,
    spec_source_url,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_example_config_matches_schema() -> None:
    """config.example.json must validate against the current Config model.

    This catches schema drift: if config.example.json is updated but the
    Pydantic models are not (or vice-versa), CI will fail here before anyone
    discovers the mismatch at runtime.
    """
    example = _REPO_ROOT / "config.example.json"
    raw = json.loads(example.read_text(encoding="utf-8"))
    cfg = Config.model_validate(raw)
    assert cfg.platforms  # sanity: at least one platform must be declared


def test_example_config_documents_every_top_level_option() -> None:
    """An option absent from the example is an option nobody discovers.

    Optional fields still validate when missing, so the check above would not
    catch one that was added to the model and never written down here.
    """
    raw = json.loads((_REPO_ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert set(raw) == set(Config.model_fields)


def test_load_valid_config(config_path: Path) -> None:
    cfg = load_config(config_path)
    assert cfg.defaults.region == "us"
    assert "us" in cfg.token_helpers
    assert cfg.token_helpers["us"].args[-1] == "print('us-token')"
    svc = cfg.platforms["demo"].regions["us"].services["api"]
    assert sorted(svc.envs) == ["dev", "prod"]
    assert str(svc.envs["prod"].url).rstrip("/") == "https://api.example.com/demo"


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.json")


def test_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(p)


def test_empty_command_rejected(tmp_path: Path, config_dict: dict) -> None:
    config_dict["token_helpers"]["us"]["command"] = ""
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_dict), encoding="utf-8")
    with pytest.raises(ConfigError, match="validation"):
        load_config(p)


@pytest.mark.parametrize("timeout", [0, -1, 301])
def test_helper_timeout_must_be_within_bounds(
    tmp_path: Path, config_dict: dict, timeout: float
) -> None:
    """A helper has no interactive channel to the user, so a five-minute wait
    is a hang rather than a login in progress."""
    config_dict["token_helpers"]["us"]["timeout"] = timeout
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_dict), encoding="utf-8")
    with pytest.raises(ConfigError, match="validation"):
        load_config(p)


def test_get_deployment_returns_flat_view(config: Config) -> None:
    dep = get_deployment(config, "demo", "us", "api", "prod")
    assert dep.api_base.rstrip("/") == "https://api.example.com/demo"
    assert dep.token_helper_name == "us"
    assert dep.token_helper is not None
    assert dep.headers["x-product"] == "demo"


def test_get_deployment_shares_auth_across_envs(config: Config) -> None:
    """us/api/prod and us/api/dev bind to the same profile; only the URL differs."""
    prod = get_deployment(config, "demo", "us", "api", "prod")
    dev = get_deployment(config, "demo", "us", "api", "dev")
    assert prod.token_helper_name == dev.token_helper_name == "us"
    assert prod.api_base != dev.api_base


def test_services_token_helper_is_between_service_and_region(config_dict: dict) -> None:
    region = config_dict["platforms"]["demo"]["regions"]["us"]
    region.pop("token_helper")
    region["services"]["token_helper"] = "eu"
    cfg = Config.model_validate(config_dict)
    assert get_deployment(cfg, "demo", "us", "api", "prod").token_helper_name == "eu"


def test_default_region_must_exist(tmp_path: Path, config_dict: dict) -> None:
    config_dict["defaults"]["region"] = "mars"
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_dict), encoding="utf-8")
    with pytest.raises(ConfigError, match="defaults.region.*mars"):
        load_config(p)


def test_default_env_must_exist(tmp_path: Path, config_dict: dict) -> None:
    config_dict["defaults"]["env"] = "stage"  # not configured under us/api
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_dict), encoding="utf-8")
    with pytest.raises(ConfigError, match="defaults.env.*stage"):
        load_config(p)


def test_resolve_uses_arg_first() -> None:
    assert resolve("region", "cn", "us") == "cn"


def test_resolve_falls_back_to_default() -> None:
    assert resolve("region", None, "us") == "us"


def test_resolve_raises_when_both_none() -> None:
    with pytest.raises(ConfigError, match="missing 'region'"):
        resolve("region", None, None)


def test_missing_helper_means_unauthenticated(config: Config) -> None:
    """A deployment without a token_helper is unauthenticated."""
    assert get_deployment(config, "demo", "eu", "api", "prod").token_helper_name is None


def test_explicit_auth_reference_wins_over_region_name(config_dict: dict) -> None:
    config_dict["platforms"]["demo"]["regions"]["eu"]["token_helper"] = "us"
    cfg = Config.model_validate(config_dict)
    assert get_deployment(cfg, "demo", "eu", "api", "prod").token_helper_name == "us"


def test_most_specific_auth_reference_wins(config_dict: dict) -> None:
    demo = config_dict["platforms"]["demo"]
    demo["token_helper"] = "eu"
    demo["regions"]["us"].pop("token_helper")
    demo["regions"]["us"]["services"]["api"]["envs"]["dev"]["token_helper"] = "us"
    cfg = Config.model_validate(config_dict)
    assert get_deployment(cfg, "demo", "us", "api", "prod").token_helper_name == "eu"
    assert get_deployment(cfg, "demo", "us", "api", "dev").token_helper_name == "us"


def test_null_token_helper_does_not_stop_inheritance(config_dict: dict) -> None:
    """Null is equivalent to omitting token_helper and keeps inheritance."""
    config_dict["platforms"]["demo"]["token_helper"] = None
    cfg = Config.model_validate(config_dict)
    dep = get_deployment(cfg, "demo", "us", "api", "prod")
    assert dep.token_helper_name == "us"
    assert dep.token_helper is not None


def test_unresolvable_auth_is_reported_at_load(
    tmp_path: Path, config_dict: dict
) -> None:
    config_dict["token_helpers"] = {"other": config_dict["token_helpers"]["us"]}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_dict), encoding="utf-8")
    with pytest.raises(ConfigError, match="token helper 'us'"):
        load_config(p)


def test_unknown_auth_reference_is_reported_at_load(
    tmp_path: Path, config_dict: dict
) -> None:
    config_dict["platforms"]["demo"]["token_helper"] = "nope"
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_dict), encoding="utf-8")
    with pytest.raises(ConfigError, match="token helper 'nope'"):
        load_config(p)


def test_get_deployment_unknown_region(config: Config) -> None:
    with pytest.raises(ConfigError, match="region 'mars'"):
        get_deployment(config, "demo", "mars", "api", "prod")


def test_get_deployment_unknown_env(config: Config) -> None:
    with pytest.raises(ConfigError, match="env 'stage'"):
        get_deployment(config, "demo", "us", "api", "stage")


def test_spec_source_url_uses_canonical_env(config: Config) -> None:
    # demo/us/api: canonical_env=prod → prod URL + spec_path
    url = spec_source_url(config, "demo", "us", "api")
    assert url == "https://api.example.com/demo/swagger/v1/swagger.json"


def test_spec_source_url_explicit_env(config: Config) -> None:
    url = spec_source_url(config, "demo", "us", "api", env="dev")
    assert url == "https://api-dev.example.com/demo/swagger/v1/swagger.json"


def test_spec_source_url_unknown_service(config: Config) -> None:
    with pytest.raises(ConfigError, match="service 'nope'"):
        spec_source_url(config, "demo", "us", "nope")


# ── background refresh policy ────────────────────────────────────────────────


def test_spec_refresh_defaults_to_a_weekly_sweep(config: Config) -> None:
    assert (config.spec_refresh.auto, config.spec_refresh.interval) == (True, 7.0)
    assert config.spec_refresh.interval_seconds == 604800.0


def test_spec_refresh_accepts_a_fractional_interval(config_dict: dict) -> None:
    # Days, so a sub-day cadence stays expressible: 0.5 is twelve hours.
    cfg = Config.model_validate(config_dict | {"spec_refresh": {"interval": 0.5}})
    assert cfg.spec_refresh.interval_seconds == 43200.0


def test_turning_auto_off_keeps_the_interval(config_dict: dict) -> None:
    # The switch and the cadence are separate fields precisely so that disabling
    # the sweep does not throw away how often it used to run.
    cfg = Config.model_validate(
        config_dict | {"spec_refresh": {"auto": False, "interval": 3}}
    )
    assert (cfg.spec_refresh.auto, cfg.spec_refresh.interval) == (False, 3.0)


@pytest.mark.parametrize(
    "block",
    [
        pytest.param({"interval": 0}, id="zero is not a way to disable"),
        pytest.param({"interval": -1}, id="negative"),
        pytest.param({"interval": "weekly"}, id="not a number"),
        pytest.param({"auto": "yes"}, id="auto is not a bool"),
        pytest.param({"nope": 1}, id="unknown field"),
    ],
)
def test_a_bad_spec_refresh_block_is_rejected(config_dict: dict, block: dict) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(config_dict | {"spec_refresh": block})
