"""City config pack loader.

A city config pack is a directory containing a city.yaml (schema documented in
docs/canonical-model.md §4) plus data files it references (canonical map CSVs).
The engine (data_model.py) is city-agnostic; everything city-specific lives in
the pack. Select a pack via the CITY_CONFIG env var (path to a city.yaml);
defaults to the Louisville pack.
"""

import csv
import os

import yaml

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
