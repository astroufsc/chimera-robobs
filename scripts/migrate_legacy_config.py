#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""One-time converter for legacy chimera-robobs YAML input files.

The chimera-robobs CLI only accepts the canonical snake_case dialect
(``scheduling_algorithm: recurrent``, ``pre_actions``/``post_actions``,
``slot_len``, ...).  This standalone script converts the legacy dialect
(camelCase keys, numeric ``schedalgorith`` codes, ``pos-actions``,
``slotLen``, ``{targetRa}``-style template placeholders) into the canonical
form.  It is deliberately self-contained (needs only pyyaml) and lives
outside the package: once a site's files are converted it is never needed
again.

Usage:

    python scripts/migrate_legacy_config.py legacy.yaml [...] [-o OUTDIR]

Without ``-o`` the converted YAML is printed to stdout; with ``-o`` each
file is written to the given directory under its original name.  The file
kind (project / block / pid-config) is detected from the content.
"""

import argparse
import pathlib
import sys

import yaml

#: numeric ``schedalgorith`` codes -> canonical algorithm names
ALGORITHM_NAMES = {
    0: "higher",
    1: "extinction_monitor",
    2: "timed",
    3: "recurrent",
    4: "time_sequence",
}

#: accepted algorithm spellings (legacy short names included) -> id
ALGORITHM_ALIASES = {name: aid for aid, name in ALGORITHM_NAMES.items()} | {
    "hig": 0,
    "std": 1,
    "timesequence": 4,
}

#: legacy project block-parameter keys -> canonical keys
BLOCKPAR_KEYS = {
    "maxairmass": "max_airmass",
    "minairmass": "min_airmass",
    "maxmoonBright": "max_moon_bright",
    "minmoonBright": "min_moon_bright",
    "minmoonDist": "min_moon_distance",
    "maxseeing": "max_seeing",
    "cloudcover": "cloud_cover",
    "schedalgorith": "scheduling_algorithm",
    "sched_algorithm": "scheduling_algorithm",
    "applyextcorr": "apply_ext_corr",
}

#: legacy action keys -> canonical keys
ACTION_KEYS = {
    "imageType": "image_type",
    "objectName": "object_name",
    "targetName": "target_name",
}

#: legacy template placeholders (inside string action values) -> canonical
PLACEHOLDERS = {
    "{targetRa}": "{target_ra}",
    "{targetDec}": "{target_dec}",
    "{targetEpoch}": "{target_epoch}",
    "{targetMag}": "{target_mag}",
    "{magFilter}": "{mag_filter}",
    "{lastObservation}": "{last_observation}",
    "{objid}": "{target_id}",
    "{bparid}": "{block_par_id}",
}

#: legacy pid-config keys -> canonical keys
PID_CONFIG_KEYS = {
    "slotLen": "slot_len",
    "nstars": "n_stars",
    "nairmass": "n_airmass",
}

PRE_ACTION_SECTIONS = ("pre_actions", "pre-actions")
POST_ACTION_SECTIONS = ("post_actions", "pos-actions", "post-actions")


def parse_algorithm(value) -> str:
    """Return the canonical algorithm name for a legacy id or name."""
    if isinstance(value, bool):
        raise ValueError(f"invalid scheduling algorithm: {value!r}")
    if isinstance(value, int | float):
        aid = int(value)
    else:
        text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        if text.lstrip("-").isdigit():
            aid = int(text)
        else:
            try:
                aid = ALGORITHM_ALIASES[text]
            except KeyError:
                raise ValueError(f"unknown scheduling algorithm {value!r}") from None
    if aid not in ALGORITHM_NAMES:
        raise ValueError(f"unknown scheduling algorithm id {aid}")
    return ALGORITHM_NAMES[aid]


def migrate_blockpar(block: dict) -> dict:
    converted = {}
    for key, value in block.items():
        if key in ("id", "pid", "name"):
            converted[key] = value
            continue
        canonical = BLOCKPAR_KEYS.get(key, key)
        if canonical in ("scheduling_algorithm",):
            converted[canonical] = parse_algorithm(value)
        elif canonical == "apply_ext_corr":
            converted[canonical] = bool(value)
        else:
            converted[canonical] = value
    return converted


def migrate_action(action: dict) -> dict:
    converted = {}
    for key, value in action.items():
        if isinstance(value, str):
            for legacy, canonical in PLACEHOLDERS.items():
                value = value.replace(legacy, canonical)
        converted[ACTION_KEYS.get(key, key)] = value
    return converted


def _section(doc: dict, names) -> list:
    for name in names:
        if name in doc:
            return doc[name] or []
    return []


def migrate_document(doc: dict) -> tuple[str, dict]:
    """Convert one YAML document; returns ``(kind, converted)``."""
    if "project" in doc:
        converted = {"project": doc["project"]}
        if "observing_blocks" in doc:
            converted["observing_blocks"] = {
                name: migrate_blockpar(block)
                for name, block in doc["observing_blocks"].items()
            }
        return "project", converted

    if any(key in doc for key in PRE_ACTION_SECTIONS + POST_ACTION_SECTIONS):
        converted = {}
        pre = _section(doc, PRE_ACTION_SECTIONS)
        post = _section(doc, POST_ACTION_SECTIONS)
        if pre:
            converted["pre_actions"] = [migrate_action(a) for a in pre]
        if post:
            converted["post_actions"] = [migrate_action(a) for a in post]
        return "block", converted

    return "pid-config", {
        PID_CONFIG_KEYS.get(key, key): value for key, value in doc.items()
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("files", nargs="+", help="legacy YAML files to convert")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="write converted files to this directory (default: print to stdout)",
    )
    args = parser.parse_args(argv)

    status = 0
    outdir = pathlib.Path(args.output) if args.output else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)

    for target in args.files:
        path = pathlib.Path(target)
        try:
            with open(path) as fp:
                doc = yaml.safe_load(fp)
            kind, converted = migrate_document(doc)
        except (OSError, yaml.YAMLError, ValueError, KeyError) as e:
            print(f"FAIL  {path}: {e}", file=sys.stderr)
            status = 1
            continue
        text = yaml.safe_dump(converted, sort_keys=False, default_flow_style=False)
        if outdir:
            destination = outdir / path.name
            destination.write_text(text)
            print(f"OK    {path} -> {destination} ({kind})", file=sys.stderr)
        else:
            print(f"# migrated {kind} file from {path}")
            print(text)
    return status


if __name__ == "__main__":
    sys.exit(main())
