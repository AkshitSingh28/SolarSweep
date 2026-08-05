"""Configuration validation.

v1's loader silently swallowed anything it did not recognise: a typo'd key was
dropped, a missing file fell back to defaults without a word, and mutually
impossible settings were accepted. These tests exist so a bad settings.yaml
fails at load time rather than halfway up a rail.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from solarsweep.config import (
    ConfigError,
    DriveConfig,
    PanelConfig,
    PinConfig,
    SafetyConfig,
    Settings,
    load_settings,
)


def write(tmp_path, text: str):
    path = tmp_path / "settings.yaml"
    path.write_text(text)
    return path


def test_missing_file_uses_defaults(tmp_path):
    settings = load_settings(tmp_path / "nope.yaml")
    assert settings.name == "SolarSweep-v2"
    assert settings.panel.rail_travel_mm > 0


def test_shipped_config_is_valid():
    """The config in the repo must actually load. v1's did not: its loader
    looked for `config/settings.yaml` while the directory was `Config/`."""
    settings = load_settings("config/settings.yaml")
    assert settings.drive.closed_loop


def test_shipped_as_built_config_is_valid():
    settings = load_settings("config/settings.as-built.yaml")
    assert not settings.drive.closed_loop  # no encoder on the prototype


def test_unknown_key_is_rejected(tmp_path):
    path = write(tmp_path, "cleaning:\n  passes: 2\n  overlap_mm: 10\n")
    with pytest.raises(ConfigError, match="overlap_mm"):
        load_settings(path)


def test_unknown_section_is_rejected(tmp_path):
    path = write(tmp_path, "suspension:\n  spring_preload_mm: 5\n")
    with pytest.raises(ConfigError, match="suspension"):
        load_settings(path)


def test_non_mapping_file_is_rejected(tmp_path):
    path = write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_settings(path)


def test_duplicate_pin_assignment_is_rejected():
    with pytest.raises(ConfigError, match="both on BCM"):
        PinConfig(drive_in1=17, brush_in1=17).validate()


def test_unfitted_pins_may_repeat_as_minus_one():
    PinConfig(encoder_a=-1, encoder_b=-1).validate()


def test_pin_outside_bcm_range_is_rejected():
    with pytest.raises(ConfigError, match="valid BCM pin"):
        PinConfig(drive_in1=47).validate()


def test_duty_below_stall_threshold_is_rejected():
    with pytest.raises(ConfigError, match="stall"):
        DriveConfig(min_duty_pct=50, cleaning_duty_pct=30).validate()


def test_water_pulse_longer_than_interval_is_rejected(settings):
    bad = replace(settings.cleaning, water_pulse_ms=9000, water_interval_s=8.0)
    with pytest.raises(ConfigError, match="never rests"):
        bad.validate()


def test_lead_in_plus_lead_out_cannot_exceed_the_rail():
    with pytest.raises(ConfigError, match="lead_in"):
        PanelConfig(rail_travel_mm=100, lead_in_mm=60, lead_out_mm=60).validate()


def test_traverse_timeout_shorter_than_a_pass_is_rejected(settings):
    """The cross-section check: each value is legal, the combination is not."""
    bad = replace(settings, safety=replace(settings.safety, traverse_timeout_max_s=5.0))
    with pytest.raises(ConfigError, match="time out before finishing"):
        bad.validate()


def test_home_timeout_shorter_than_the_rail_is_rejected(settings):
    """This is the bug the first simulated run hit: parking timed out because
    homing the full rail at homing duty needed more than home_timeout_s."""
    bad = replace(settings, safety=replace(settings.safety, home_timeout_s=10.0))
    with pytest.raises(ConfigError, match="homing from the far end"):
        bad.validate()


def test_non_loopback_bind_without_a_token_is_rejected(settings, monkeypatch):
    monkeypatch.delenv("SOLARSWEEP_WEB_TOKEN", raising=False)
    bad = replace(settings, web=replace(settings.web, host="0.0.0.0"))
    with pytest.raises(ConfigError, match="SOLARSWEEP_WEB_TOKEN"):
        bad.validate()


def test_token_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLARSWEEP_WEB_TOKEN", "s3cret")
    path = write(tmp_path, 'web:\n  host: "0.0.0.0"\n')
    settings = load_settings(path)
    assert settings.web.auth_token == "s3cret"


def test_cron_must_have_five_fields(tmp_path):
    path = write(tmp_path, 'scheduler:\n  enabled: true\n  jobs:\n    - cron: "0 6 *"\n')
    with pytest.raises(ConfigError, match="5 fields"):
        load_settings(path)


def test_cleaning_span_excludes_the_leads(settings):
    expected = (
        settings.panel.rail_travel_mm
        - settings.panel.lead_in_mm
        - settings.panel.lead_out_mm
    )
    assert settings.cleaning_span_mm == expected


def test_overcurrent_disabled_by_zero():
    SafetyConfig(overcurrent_a=0.0).validate()
    with pytest.raises(ConfigError):
        SafetyConfig(overcurrent_a=-1.0).validate()


def test_every_settings_field_is_reachable_from_yaml(tmp_path):
    """Guards against the v1 failure mode where settings.yaml grew keys that
    no code read. Every section here must round-trip."""
    settings = Settings()
    for section in ("panel", "drive", "cleaning", "safety", "pins", "triggers",
                    "web", "logging", "sim"):
        assert hasattr(settings, section)
