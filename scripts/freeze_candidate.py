#!/usr/bin/env python3
"""Freeze or verify CivicTrace runtime/test content without circular commit IDs."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GROUP_DIRS = {
    'source': ('civictrace', 'data'),
    'tests': ('tests',),
    'harness': ('scripts',),
}
BEHAVIOR_FILES = (
    'requirements.txt', 'requirements-dev.txt', 'requirements.lock', 'requirements-dev.lock',
    'Dockerfile', '.dockerignore', '.env.example', '.gitattributes', 'pytest.ini',
)
EXCLUDED_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', '.ruff_cache', '.private-evidence'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.key', '.pem', '.env'}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> str:
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def excluded(path: Path) -> bool:
    return path.suffix.lower() in EXCLUDED_SUFFIXES or any(part in EXCLUDED_DIRS for part in path.parts)


def collect_directory(name: str) -> dict[str, str]:
    output: dict[str, str] = {}
    base = ROOT / name
    if not base.is_dir():
        return output
    for path in sorted(base.rglob('*')):
        if path.is_file():
            rel = path.relative_to(ROOT)
            if not excluded(rel):
                output[rel.as_posix()] = sha(path.read_bytes())
    return output


def collect() -> dict[str, dict[str, str]]:
    groups: dict[str, dict[str, str]] = {}
    for group, directories in GROUP_DIRS.items():
        merged: dict[str, str] = {}
        for directory in directories:
            merged.update(collect_directory(directory))
        groups[group] = merged
    groups['behavior'] = {
        name: sha((ROOT / name).read_bytes()) for name in BEHAVIOR_FILES if (ROOT / name).is_file()
    }
    return groups


def runtime_digest(groups: dict[str, dict[str, str]]) -> str:
    entries = sorted((path, value) for mapping in groups.values() for path, value in mapping.items())
    return sha('\n'.join(f'{path}={value}' for path, value in entries).encode('utf-8'))


def build(label: str, review_artifact: Path | None) -> dict:
    groups = collect()
    review: dict = {'present': False}
    if review_artifact is not None:
        review_artifact = review_artifact.resolve()
        if not review_artifact.is_file():
            raise RuntimeError('Review artifact does not exist')
        review = {
            'present': True, 'path': str(review_artifact),
            'sha256': sha(review_artifact.read_bytes()), 'bytes': review_artifact.stat().st_size,
            'note': 'External review artifact; keep outside release package and byte-identical.',
        }
    status = [line for line in git('status', '--porcelain').splitlines() if line]
    return {
        'schema': 'civictrace-candidate-manifest/1',
        'label': label,
        'created_at_utc': datetime.now(UTC).isoformat(),
        'git': {
            'revision_before_manifest': git('rev-parse', 'HEAD'),
            'branch': git('branch', '--show-current'),
            'remotes': git('remote').splitlines(),
            'working_tree_porcelain': status,
        },
        'content_hashes': groups,
        'runtime_content_digest': runtime_digest(groups),
        'counts': {name: len(mapping) for name, mapping in groups.items()},
        'review_artifact': review,
        'nonclaims': ['Content hashes detect change; they do not authenticate author or independent acceptance.'],
    }


def verify(path: Path) -> list[str]:
    stored = json.loads(path.read_text(encoding='utf-8'))
    current = collect()
    findings: list[str] = []
    for group, expected in stored['content_hashes'].items():
        actual = current.get(group, {})
        for name, value in expected.items():
            if name not in actual:
                findings.append(f'{name}: removed')
            elif actual[name] != value:
                findings.append(f'{name}: changed')
        for name in actual:
            if name not in expected:
                findings.append(f'{name}: added')
    if runtime_digest(stored['content_hashes']) != stored['runtime_content_digest']:
        findings.append('stored runtime digest is internally inconsistent')
    review = stored.get('review_artifact', {})
    if review.get('present'):
        target = Path(review['path'])
        if not target.is_file() or sha(target.read_bytes()) != review['sha256']:
            findings.append('external review artifact missing or changed')
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', default='candidate')
    parser.add_argument('--out')
    parser.add_argument('--verify')
    parser.add_argument('--review-artifact')
    args = parser.parse_args()
    if args.verify:
        findings = verify(Path(args.verify))
        if findings:
            print('CANDIDATE CHANGED:')
            for finding in findings:
                print(f'  {finding}')
            return 1
        print('CANDIDATE UNCHANGED: runtime, tests, harnesses and behavior files match the manifest')
        return 0
    record = build(args.label, Path(args.review_artifact) if args.review_artifact else None)
    output = Path(args.out) if args.out else ROOT / 'evidence' / 'candidates' / f'{args.label}.json'
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f"Frozen {args.label}: {record['runtime_content_digest']}")
    print(f'Record: {output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
