"""Read this installation's .env explicitly; ambient exports are not configuration."""
from pathlib import Path
from dotenv import dotenv_values
ROOT = Path(__file__).resolve().parents[1]
_values = dotenv_values(ROOT / '.env', interpolate=False)

def load(path):
    global _values
    _values = dotenv_values(Path(path), interpolate=False)

def setting(name, default=None):
    value = _values.get(name)
    if value is None or (value == '' and name.endswith(('_DB','_DATA_PATH'))):
        value = default
    if name.endswith(('_DB','_DATA_PATH')) and value is not None:
        path = Path(value)
        return str(path if path.is_absolute() else ROOT / path)
    return value
