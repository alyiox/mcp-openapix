"""Tests for config.d/ drop-in files merged over config.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_openapix.config import ConfigError, get_deployment, load_config


def _service(url: str) -> dict:
    return {
        "spec_path": "/openapi.json",
        "envs": {"prod": {"url": url}},
    }


def _platform(url: str = "https://api.example.com/items") -> dict:
    return {"regions": {"us": {"services": {"items": _service(url)}}}}


def _write(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
    )
    return path


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return _write(tmp_path / "config.json", {"platforms": {"acme": _platform()}})


def test_no_config_d_changes_nothing(base: Path) -> None:
    cfg = load_config(base)
    assert sorted(cfg.platforms) == ["acme"]
    assert cfg.platform_errors == {} and cfg.file_errors == {}


def test_drop_ins_merge_over_the_base(base: Path) -> None:
    _write(base.parent / "config.d" / "b.json", {"platforms": {"beta": _platform()}})
    _write(base.parent / "config.d" / "a.json", {"platforms": {"alpha": _platform()}})
    cfg = load_config(base)
    assert sorted(cfg.platforms) == ["acme", "alpha", "beta"]


def test_base_may_omit_platforms(tmp_path: Path) -> None:
    base = _write(tmp_path / "config.json", {"response_cache_ttl": 60})
    _write(tmp_path / "config.d" / "acme.json", {"platforms": {"acme": _platform()}})
    cfg = load_config(base)
    assert sorted(cfg.platforms) == ["acme"]
    assert cfg.response_cache_ttl == 60


def test_only_json_directly_in_config_d_is_read(base: Path) -> None:
    d = base.parent / "config.d"
    _write(d / "old.json.bak", "{broken")
    _write(d / ".x.json.swp", "{broken")
    _write(d / "nested" / "deep.json", {"platforms": {"deep": _platform()}})
    cfg = load_config(base)
    assert sorted(cfg.platforms) == ["acme"]
    assert cfg.file_errors == {}


def test_a_later_file_overrides_one_leaf(base: Path) -> None:
    local = _write(
        base.parent / "config.d" / "zz-local.json",
        {
            "platforms": {
                "acme": {
                    "regions": {
                        "us": {
                            "services": {
                                "items": {
                                    "envs": {"prod": {"url": "http://localhost:8080"}}
                                }
                            }
                        }
                    }
                }
            }
        },
    )
    cfg = load_config(base)
    svc = cfg.platforms["acme"].regions["us"].services["items"]
    assert str(svc.envs["prod"].url).rstrip("/") == "http://localhost:8080"
    assert svc.spec_path == "/openapi.json"  # siblings survive the merge
    assert cfg.overrides == [
        (
            "platforms.acme.regions.us.services.items.envs.prod.url",
            str(base),
            str(local),
        )
    ]


def test_restating_the_same_value_is_not_an_override(base: Path) -> None:
    _write(base.parent / "config.d" / "same.json", {"platforms": {"acme": _platform()}})
    assert load_config(base).overrides == []


def test_a_later_file_overrides_one_default(tmp_path: Path) -> None:
    base = _write(
        tmp_path / "config.json",
        {
            "defaults": {"platform": "acme", "username": "me"},
            "platforms": {"acme": _platform()},
        },
    )
    _write(tmp_path / "config.d" / "me.json", {"defaults": {"username": "you"}})
    cfg = load_config(base)
    assert cfg.defaults.platform == "acme" and cfg.defaults.username == "you"


def test_any_setting_may_live_in_a_drop_in(base: Path) -> None:
    _write(
        base.parent / "config.d" / "server.json",
        {
            "headers": {"x-tenant": "acme"},
            "truncate_threshold": 100,
            "spec_refresh": {"auto": False},
            "defaults": {"platform": "acme"},
        },
    )
    cfg = load_config(base)
    assert cfg.headers == {"x-tenant": "acme"}
    assert cfg.truncate_threshold == 100
    assert cfg.spec_refresh.auto is False
    assert cfg.defaults.platform == "acme"


def test_headers_merge_across_files(tmp_path: Path) -> None:
    base = _write(tmp_path / "config.json", {"headers": {"accept": "application/json"}})
    _write(tmp_path / "config.d" / "h.json", {"headers": {"x-tenant": "acme"}})
    cfg = load_config(base)
    assert cfg.headers == {"accept": "application/json", "x-tenant": "acme"}


def test_helpers_may_live_in_their_own_drop_in(tmp_path: Path) -> None:
    base = _write(tmp_path / "config.json", {})
    d = tmp_path / "config.d"
    _write(d / "token-helpers.json", {"token_helpers": {"us": {"command": "helper"}}})
    platform = _platform()
    platform["token_helper"] = "us"
    _write(d / "acme.json", {"platforms": {"acme": platform}})
    cfg = load_config(base)
    assert get_deployment(cfg, "acme", "us", "items", "prod").token_helper_name == "us"


def test_a_skipped_helper_file_fails_the_platforms_using_it(tmp_path: Path) -> None:
    base = _write(tmp_path / "config.json", {})
    d = tmp_path / "config.d"
    helpers = _write(d / "token-helpers.json", '{"token_helpers": {"us": ,}}')
    platform = _platform()
    platform["token_helper"] = "us"
    _write(d / "acme.json", {"platforms": {"acme": platform, "public": _platform()}})
    cfg = load_config(base)
    assert sorted(cfg.platforms) == ["public"]
    error = cfg.platform_errors["acme"]
    assert "token helper 'us'" in error and str(helpers) in error


def test_a_bad_setting_in_a_drop_in_is_fatal_naming_it(base: Path) -> None:
    bad = _write(
        base.parent / "config.d" / "helpers.json",
        {"token_helpers": {"us": {"command": ""}}},
    )
    with pytest.raises(ConfigError, match="failed validation") as e:
        load_config(base)
    assert str(bad) in str(e.value) and str(base) not in str(e.value)


def test_an_unknown_key_in_a_drop_in_is_fatal_naming_it(base: Path) -> None:
    bad = _write(base.parent / "config.d" / "typo.json", {"header": {}})
    with pytest.raises(ConfigError, match="failed validation") as e:
        load_config(base)
    assert str(bad) in str(e.value)


def test_unparseable_drop_in_costs_only_itself(base: Path) -> None:
    bad = _write(base.parent / "config.d" / "bad.json", '{"platforms": {,}}')
    _write(base.parent / "config.d" / "good.json", {"platforms": {"good": _platform()}})
    cfg = load_config(base)
    assert sorted(cfg.platforms) == ["acme", "good"]
    assert "not valid JSON" in cfg.file_errors[str(bad)]
    with pytest.raises(ConfigError, match="could not be read: .*bad.json"):
        get_deployment(cfg, "lost", "us", "items", "prod")


@pytest.mark.parametrize(
    ("content", "reason"), [("[]", "JSON object"), ("\xff", "not readable")]
)
def test_a_drop_in_that_is_not_an_object_is_skipped(
    base: Path, content: str, reason: str
) -> None:
    bad = base.parent / "config.d" / "bad.json"
    bad.parent.mkdir()
    bad.write_bytes(content.encode("latin-1"))
    cfg = load_config(base)
    assert sorted(cfg.platforms) == ["acme"]
    assert reason in cfg.file_errors[str(bad)]


def test_an_empty_drop_in_is_fine(base: Path) -> None:
    _write(base.parent / "config.d" / "empty.json", {})
    cfg = load_config(base)
    assert sorted(cfg.platforms) == ["acme"] and cfg.file_errors == {}


def test_invalid_platform_costs_only_itself(base: Path) -> None:
    broken = _platform()
    broken["regions"]["us"]["services"]["items"]["envs"]["prod"]["url"] = "nope"
    src = _write(
        base.parent / "config.d" / "mixed.json",
        {"platforms": {"broken": broken, "fine": _platform()}},
    )
    cfg = load_config(base)
    assert sorted(cfg.platforms) == ["acme", "fine"]
    error = cfg.platform_errors["broken"]
    assert str(src) in error and "url" in error and "restart" in error
    with pytest.raises(ConfigError, match="'broken' failed to load"):
        get_deployment(cfg, "broken", "us", "items", "prod")


def test_invalid_platform_in_base_costs_only_itself(tmp_path: Path) -> None:
    base = _write(
        tmp_path / "config.json",
        {"platforms": {"broken": {"regions": {"us": 1}}, "fine": _platform()}},
    )
    cfg = load_config(base)
    assert sorted(cfg.platforms) == ["fine"]
    assert "broken" in cfg.platform_errors


def test_defaults_into_a_failed_platform_are_dropped(tmp_path: Path) -> None:
    base = _write(
        tmp_path / "config.json",
        {
            "defaults": {"platform": "broken", "region": "us", "username": "me"},
            "platforms": {"fine": _platform()},
        },
    )
    _write(tmp_path / "config.d" / "b.json", {"platforms": {"broken": {"x": 1}}})
    cfg = load_config(base)
    assert cfg.defaults.platform is None and cfg.defaults.region is None
    assert cfg.defaults.username == "me"


def test_unknown_default_platform_is_still_fatal(tmp_path: Path) -> None:
    base = _write(
        tmp_path / "config.json",
        {"defaults": {"platform": "typo"}, "platforms": {"fine": _platform()}},
    )
    with pytest.raises(ConfigError, match="defaults.platform='typo'"):
        load_config(base)


def test_check_config_exits_non_zero_on_any_failure(
    base: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mcp_openapix import config, server

    monkeypatch.setattr(config, "default_config_path", lambda: base)
    assert server._check_config() == 0
    assert "ok     acme" in capsys.readouterr().out

    _write(
        base.parent / "config.d" / "zz.json",
        {"platforms": {"acme": _platform("https://b.example.com")}},
    )
    assert server._check_config() == 0
    assert "override  platforms.acme.regions.us.services.items.envs.prod.url" in (
        capsys.readouterr().out
    )

    _write(base.parent / "config.d" / "bad.json", "{")
    assert server._check_config() == 1
    assert "bad.json" in capsys.readouterr().out
