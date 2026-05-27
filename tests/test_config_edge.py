"""Config-loading edge cases not covered by test_config.py.

Covers: non-mapping root, empty file, missing required gateway keys, optional
stop/start commands, and the state_dir/log_file defaults.
"""
from gateway_ctl import ConfigError, load_config
import pytest


def test_non_mapping_root_rejected(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(cfg_file)


def test_empty_file_has_no_gateways(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("")
    # yaml.safe_load("") -> None -> {} -> no gateways key.
    with pytest.raises(ConfigError, match="at least one"):
        load_config(cfg_file)


def test_missing_required_gateway_key_raises_keyerror(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
state_dir: state
log_file: watchdog.log
gateways:
  - name: primary
    port: 4001
    skip_file: primary.skip
"""  # restart_cmd missing
    )
    with pytest.raises(KeyError):
        load_config(cfg_file)


def test_optional_stop_start_cmds_parsed(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
state_dir: state
log_file: watchdog.log
gateways:
  - name: primary
    port: 4001
    restart_cmd: "echo restart"
    skip_file: primary.skip
    stop_cmd: "echo stop"
    start_cmd: "echo start"
"""
    )
    cfg = load_config(cfg_file)
    gw = cfg.get("primary")
    assert gw.stop_cmd == "echo stop"
    assert gw.start_cmd == "echo start"


def test_stop_start_default_to_none(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
state_dir: state
log_file: watchdog.log
gateways:
  - name: primary
    port: 4001
    restart_cmd: "echo restart"
    skip_file: primary.skip
"""
    )
    gw = load_config(cfg_file).get("primary")
    assert gw.stop_cmd is None
    assert gw.start_cmd is None


def test_state_dir_and_log_file_defaults(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
gateways:
  - name: primary
    port: 4001
    restart_cmd: "a"
    skip_file: primary.skip
"""
    )
    cfg = load_config(cfg_file)
    # Defaults rooted at the config file's directory.
    assert cfg.state_dir == (tmp_path / "state").resolve()
    assert cfg.log_file == (tmp_path / "watchdog.log").resolve()


def test_port_coerced_to_int(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
state_dir: state
log_file: watchdog.log
gateways:
  - name: primary
    port: "4001"
    restart_cmd: "a"
    skip_file: primary.skip
"""
    )
    gw = load_config(cfg_file).get("primary")
    assert gw.port == 4001
    assert isinstance(gw.port, int)
