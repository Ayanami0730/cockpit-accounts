#!/usr/bin/env python3
"""Check publishable file names and accidental credential material without printing values."""
from pathlib import Path
import re
import subprocess
import sys
root = Path(__file__).resolve().parents[1]
files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
blocked_names = {'auth.json', 'credentials.json', '.env', '.DS_Store', 'registry.json'}
patterns = [
    ('private-key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]{80,}-----END')),
    ('github-token', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{36,}\b')),
    ('anthropic-key', re.compile(r'\bsk-ant-api\d+-[A-Za-z0-9_-]{40,}\b')),
]
issues = []
count = 0
for name in files:
    if not name:
        continue
    p = root / name
    if p.is_symlink():
        issues.append((name, 'symlink: review target before publishing'))
        continue
    if p.name in blocked_names or p.suffix in {'.db', '.sqlite', '.sqlite3', '.log', '.pem', '.key'}:
        issues.append((name, 'runtime or credential file'))
    if not p.is_file():
        continue
    count += 1
    raw = p.read_bytes()
    if b'\0' in raw:
        continue
    content = raw.decode('utf-8', errors='replace')
    for category, pattern in patterns:
        if pattern.search(content):
            issues.append((name, category))
for name, category in issues:
    print(f'{name}: {category}')
print(f'Checked {count} tracked files; {len(issues)} findings. Values are never printed.')
print('This guard is not a proof that all personal data is absent; review release contents and use a secret scanner too.')
sys.exit(bool(issues))
