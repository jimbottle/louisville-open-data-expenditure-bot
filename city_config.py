"""City config pack loader.

A city config pack is a directory containing a city.yaml (schema documented in
docs/canonical-model.md §4) plus data files it references (canonical map CSVs).
The engine (data_model.py) is city-agnostic; everything city-specific lives in
the pack. Select a pack via the CITY_CONFIG env var (path to a city.yaml);
defaults to the Louisville pack.
"""

import csv
import logging
import os
import re

import yaml

log = logging.getLogger("city_config")

# Placeholder names a city pack's data_facts may use. They are filled from the
# loaded data at prompt-build time; a fact still containing one of these after
# substitution is dropped rather than shipped with a raw placeholder. Braces
# holding anything else are treated as ordinary prose.
KNOWN_PLACEHOLDERS = ("first_year", "newest_year", "in_progress_year", "last_complete_year")

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cities", "louisville", "city.yaml"
)


class CityConfig:
    def __init__(self, raw: dict, base_dir: str):
        self.raw = raw
        self.base_dir = base_dir
        self.city = raw.get("city", {})
        self.expenditures = raw.get("expenditures", {})
        self.canonicalization = raw.get("canonicalization", [])
        self.data_quality = raw.get("data_quality", {})
        self.enrichment_tables = raw.get("enrichment_tables", {})
        self.summaries = raw.get("summaries", [])
        self.dictionary = raw.get("dictionary", {})
        self.data_facts = raw.get("data_facts", [])

    def data_facts_for(self, values: dict | None = None) -> list:
        """Data facts with {placeholders} resolved from `values`.

        Substitution happens HERE so no consumer can leak a raw placeholder
        into a prompt. Uses plain replacement (not str.format) so pack prose
        containing literal braces can't crash startup, and any fact still
        holding an unresolved {placeholder} is dropped (with a warning)
        rather than shipped — a missing fact is safe, a malformed one is not.
        Ordinary prose braces are left alone; only identifier-shaped
        placeholders count as unresolved.
        """
        out = []
        for fact in self.data_facts:
            text = fact
            for key, val in (values or {}).items():
                if val is not None:
                    text = text.replace("{" + key + "}", str(val))
            # Only the documented placeholder names can leave a fact
            # unusable; any other braces are ordinary prose and pass through.
            leftover = next(
                (p for p in KNOWN_PLACEHOLDERS if "{" + p + "}" in text), None
            )
            if leftover:
                log.warning(
                    "Dropping city data_fact with unresolved {%s}: %.60s", leftover, text
                )
                continue
            out.append(text)
        return out

    @property
    def title(self) -> str:
        return self.city.get("title") or f"{self.city.get('name', 'City')} Open Data"

    def load_map(self, filename: str) -> dict:
        """Load a two-column canonical-map CSV (header row skipped) from the pack."""
        out = {}
        with open(os.path.join(self.base_dir, filename), newline="") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 2:
                    out[row[0]] = row[1]
        return out

    @property
    def labels(self) -> dict:
        """table -> {column: human label} (legacy ALL_LABELS shape)."""
        return {t: e.get("labels", {}) for t, e in self.dictionary.items()}

    @property
    def data_dictionary(self) -> dict:
        """table -> {description, record_scope, joins, columns} (legacy DATA_DICTIONARY shape)."""
        out = {}
        for t, e in self.dictionary.items():
            d = {k: e[k] for k in ("description", "record_scope", "joins") if k in e}
            d["columns"] = e.get("columns", {})
            out[t] = d
        return out


def load_city_config(path: str | None = None) -> CityConfig:
    path = path or os.environ.get("CITY_CONFIG") or DEFAULT_CONFIG
    with open(path) as f:
        raw = yaml.safe_load(f)
    return CityConfig(raw, os.path.dirname(os.path.abspath(path)))
