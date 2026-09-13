"""Neutral empty startup and a validated pointer to the selected website data."""
import json
from pathlib import Path
from .rag_settings import atomic_json

def selected_directory(data):
    data = Path(data).resolve()
    pointer = data / 'active_site.json'
    if not pointer.exists():
        return data
    folder = json.loads(pointer.read_text(encoding='utf-8'))['directory']
    target = (data / folder).resolve()
    if target.parent != data / 'site_builds' or not target.is_dir():
        raise ValueError('Invalid active website directory')
    return target

def initialize(data):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    current = selected_directory(data)
    defaults = {'approved_sources.json':{'records':[]},'ontology.json':{'version':'empty','relationships':[],'aliases':[]},'contacts.json':[]}
    for name,value in defaults.items():
        path=current/name
        if not path.exists():
            if current != data.resolve():
                raise ValueError('Active website files are missing')
            atomic_json(path,value)
