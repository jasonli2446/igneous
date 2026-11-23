"""api/.env.example documents every setting `Settings` defines, and only those, each with its default.

The expected names and values come from `Settings` when the tests run, so adding, removing or renaming a setting,
or changing a default, fails here until the template says the same.
"""
import os
import re

from igneous.config import API_DIR, Settings

TEMPLATE = API_DIR / ".env.example"
PREFIX = Settings.model_config["env_prefix"]
# A setting line, commented out (`# IGNEOUS_NAME=value`) or not.
SETTING = re.compile(rf"^(?:# )?({PREFIX}[A-Z0-9_]+)=(.*)$")


def _documented() -> dict[str, tuple[str, str]]:
    """Each setting the template names: the value it shows and the line above it."""
    found: dict[str, tuple[str, str]] = {}
    above = ""
    for line in TEMPLATE.read_text().splitlines():
        if match := SETTING.match(line):
            name, value = match.groups()
            assert name not in found, f"{name} appears twice in {TEMPLATE.name}"
            found[name] = (value, above)
        above = line
    return found


def _field(env_name: str) -> str:
    return env_name.removeprefix(PREFIX).lower()


def _env_names() -> set[str]:
    return {PREFIX + field.upper() for field in Settings.model_fields}


def test_template_names_every_setting_and_nothing_else():
    named, defined = set(_documented()), _env_names()
    assert not defined - named, f"settings missing from {TEMPLATE.name}: {sorted(defined - named)}"
    assert not named - defined, f"{TEMPLATE.name} names settings that don't exist: {sorted(named - defined)}"


def test_every_setting_has_a_comment_saying_what_it_means():
    undescribed = [name for name, (_, above) in _documented().items()
                   if not above.startswith("# ") or SETTING.match(above)]
    assert not undescribed, f"no comment line above {undescribed} in {TEMPLATE.name}"


def test_the_template_copied_with_every_line_uncommented_changes_no_setting(tmp_path, monkeypatch):
    for key in [k for k in os.environ if k.upper().startswith(PREFIX)]:
        monkeypatch.delenv(key)
    shown = {name: value for name, (value, _) in _documented().items() if name in _env_names()}
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{name}={value}\n" for name, value in shown.items()))

    defaults, copied = Settings(_env_file=None), Settings(_env_file=env_file)

    changed = {name: value for name, value in shown.items()
               if getattr(copied, _field(name)) != getattr(defaults, _field(name))}
    assert not changed, f"{TEMPLATE.name} shows values that are not the defaults: {changed}"
