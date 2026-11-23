"""Immutable evaluation artifacts and checked local references."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import uuid
from zoneinfo import ZoneInfo


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def identifier(prefix='run'):
    return f'{prefix}_{datetime.now(ZoneInfo("Asia/Shanghai")):%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:8]}'


def save(directory, name, value):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    with path.open('xb') as stream:
        stream.write(raw)
    return reference(directory, path)


def reference(directory, path):
    path = path.resolve(strict=True)
    relative = path.relative_to(directory.resolve()).as_posix()
    raw = path.read_bytes()
    return {'path': relative, 'sha256': digest(raw), 'bytes': len(raw)}


def read_reference(directory, ref):
    if not isinstance(ref, dict) or not re.fullmatch(r'[a-f0-9]{64}', ref.get('sha256', '')):
        raise ValueError('Invalid artifact reference')
    path = directory / ref['path']
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError('Artifact path escapes the run or contains links')
    raw = path.read_bytes()
    if digest(raw) != ref['sha256'] or len(raw) != ref['bytes']:
        raise ValueError('Artifact hash or length changed')
    return raw


def load_run(directory):
    directory = Path(directory).resolve(strict=True)
    value = json.loads((directory / 'run.json').read_bytes())
    if value.get('schemaVersion') != 1 or value.get('benchmark') != 'swe-bench':
        raise ValueError('Not a SWE-bench evaluation run')
    spec = json.loads(read_reference(directory, value['taskSpec']))
    read_reference(directory, value['gradingInput'])
    return directory, value, spec
