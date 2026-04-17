"""Tests for config loading and validation."""
from pathlib import Path

import pytest

from gateway_ctl import Config, ConfigError, load_config


def test_load_valid_config(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
state_dir: state
log_file: watchdog.log
port_probe: netstat
gateways:
  - name: primary
    port: 4001
    restart_cmd: "echo primary"
    skip_file: primary.skip
  - name: secondary
    port: 4002
    restart_cmd: "echo secondary"
    skip_file: secondary.skip
"""
    )

    cfg = load_config(cfg_file)

    assert isinstance(cfg, Config)
    assert len(cfg.gateways) == 2
    assert cfg.gateways[0].name == "primary"
    assert cfg.gateways[0].port == 4001
    assert cfg.gateways[0].skip_file.name == "primary.skip"


def test_load_config_resolves_skip_file_under_state_dir(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
state_dir: mystate
log_file: watchdog.log
gateways:
  - name: primary
    port: 4001
    restart_cmd: "echo primary"
    skip_file: primary.skip
"""
    )

    cfg = load_config(cfg_file)

    # skip_file path should be absolute, rooted at state_dir (relative to config.yaml).
    assert cfg.gateways[0].skip_file == tmp_path / "mystate" / "primary.skip"


def test_load_config_rejects_duplicate_gateway_name(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
state_dir: state
log_file: watchdog.log
gateways:
  - name: primary
    port: 4001
    restart_cmd: "a"
    skip_file: a.skip
  - name: primary
    port: 4002
    restart_cmd: "b"
    skip_file: b.skip
"""
    )

    with pytest.raises(ConfigError, match="duplicate"):
        load_config(cfg_file)


def test_load_config_rejects_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_load_config_rejects_empty_gateways(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
state_dir: state
log_file: watchdog.log
gateways: []
"""
    )

    with pytest.raises(ConfigError, match="at least one"):
        load_config(cfg_file)


def test_get_gateway_by_name(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
state_dir: state
log_file: watchdog.log
gateways:
  - name: primary
    port: 4001
    restart_cmd: "echo primary"
    skip_file: primary.skip
  - name: secondary
    port: 4002
    restart_cmd: "echo secondary"
    skip_file: secondary.skip
"""
    )

    cfg = load_config(cfg_file)

    assert cfg.get("primary").port == 4001
    assert cfg.get("secondary").port == 4002
    assert cfg.get("missing") is None


def test_load_config_defaults_port_probe_to_netstat(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
state_dir: state
log_file: watchdog.log
gateways:
  - name: primary
    port: 4001
    restart_cmd: "a"
    skip_file: primary.skip
"""
    )

    cfg = load_config(cfg_file)
    assert cfg.port_probe == "netstat"
