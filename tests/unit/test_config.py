"""Bootstrap configuration loading (§4.14)."""

from pathlib import Path

import pytest

from proskenion.config import (
    CONFIG_ENV_VAR,
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    Environment,
    load_config,
    resolve_config_path,
)

SPEC_EXAMPLE = """\
[database]
path = "/data/auditorium.db"

[server]
host = "127.0.0.1"
port = 8000

[logging]
path = "/data/logs"
"""


def write(tmp_path: Path, text: str, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_spec_example(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SPEC_EXAMPLE))
    assert config.database.path == Path("/data/auditorium.db")
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8000
    assert config.logging.path == Path("/data/logs")
    assert config.app.environment is Environment.PRODUCTION
    assert config.is_development is False


def test_app_environment_development(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SPEC_EXAMPLE + '\n[app]\nenvironment = "development"\n'))
    assert config.app.environment is Environment.DEVELOPMENT
    assert config.is_development is True


def test_app_state_dir_and_data_dir_default(tmp_path: Path) -> None:
    """§2.3: the two per-installation directories, both overridable (finding 5)."""
    config = load_config(write(tmp_path, SPEC_EXAMPLE))
    assert config.app.state_dir == Path("/srv/appliance")
    assert config.app.data_dir == Path("/data")


def test_app_data_dir_is_configurable(tmp_path: Path) -> None:
    extra = '\n[app]\nstate_dir = "/tmp/state"\ndata_dir = "/tmp/data"\n'
    config = load_config(write(tmp_path, SPEC_EXAMPLE + extra))
    assert config.app.state_dir == Path("/tmp/state")
    assert config.app.data_dir == Path("/tmp/data")


def test_server_defaults_when_section_omitted(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, '[database]\npath = "/x.db"\n[logging]\npath = "/logs"\n'))
    assert (config.server.host, config.server.port) == ("127.0.0.1", 8000)


def test_rejects_unknown_key(tmp_path: Path) -> None:
    path = write(tmp_path, SPEC_EXAMPLE.replace("host =", "bind ="))
    with pytest.raises(ConfigError, match=r"unknown key 'server\.bind'"):
        load_config(path)


def test_rejects_unknown_section(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unknown key 'smtp'"):
        load_config(write(tmp_path, SPEC_EXAMPLE + '\n[smtp]\nhost = "mail"\n'))


def test_rejects_unknown_environment(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"'app\.environment'"):
        load_config(write(tmp_path, SPEC_EXAMPLE + '\n[app]\nenvironment = "staging"\n'))


def test_rejects_missing_required_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"missing required key 'logging'"):
        load_config(write(tmp_path, '[database]\npath = "/x.db"\n'))


def test_rejects_bad_port(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"'server\.port'"):
        load_config(write(tmp_path, SPEC_EXAMPLE.replace("8000", "70000")))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.toml")


def test_invalid_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(write(tmp_path, "[database\npath = 1"))


def test_error_names_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, SPEC_EXAMPLE + "\n[extra]\n")
    with pytest.raises(ConfigError, match=str(path).replace("\\", r"\\")):
        load_config(path)


def test_env_var_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write(tmp_path, SPEC_EXAMPLE.replace("8000", "8123"), name="from-env.toml")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(path))
    assert resolve_config_path() == path
    assert load_config().server.port == 8123


def test_explicit_path_beats_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from_env = write(tmp_path, SPEC_EXAMPLE.replace("8000", "8123"), name="env.toml")
    explicit = write(tmp_path, SPEC_EXAMPLE.replace("8000", "8456"), name="cli.toml")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(from_env))
    assert load_config(explicit).server.port == 8456


def test_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    assert resolve_config_path() == DEFAULT_CONFIG_PATH
    assert DEFAULT_CONFIG_PATH == Path("/opt/auditorium/config.toml")


def test_config_is_frozen(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SPEC_EXAMPLE))
    with pytest.raises(Exception, match="frozen"):
        config.server.port = 1
    assert isinstance(config, Config)
