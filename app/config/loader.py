from pathlib import Path

import yaml


BASE_DIR = Path(__file__).resolve().parents[2]


def load_yaml(filename: str):
    path = BASE_DIR / "config" / filename
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
