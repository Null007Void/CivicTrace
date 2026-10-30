#!/usr/bin/env python3
"""Check that the runtime SBOM matches the hash-pinned lock and that every runtime component
carries a license.

This is the durable coherence check for the release runtime inventory. It answers three
questions with file-parsing only (no network, no installation):

1. do ``requirements.lock`` and ``docs/SBOM.runtime.cdx.json`` name exactly the same distributions
   at exactly the same versions, with every pin hash-pinned?
2. does every component in the SBOM carry exactly one declared license block, in a representation
   the CycloneDX 1.6 ``licenseChoice`` schema accepts -- a single SPDX identifier in
   ``license.id``, or a compound declaration in the one-element ``expression`` form? (An
   incomplete inventory is the defect CT-QC-045 corrected, and an expression written into ``id``
   is the defect CT-QC-047 corrected, so either has to fail rather than warn.)
3. with ``--installed``, does each license agree with the distribution's own declared license
   metadata as installed in the current interpreter? That is the authoritative source the SBOM
   blocks were derived from, so the check is only complete when it runs under the lane venv.

Exit status is 0 only when every check passes; any failure prints ``FAIL:`` lines and a non-zero
status. Counts printed are measured from the files, never hard-coded.

Usage
-----
    ./.venv/Scripts/python.exe scripts/check_sbom_coherence.py --installed
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)\s*\\?\s*$')
_HASH = re.compile(r'--hash=sha256:([0-9a-f]{64})')
# An SPDX id, or an SPDX expression of ids joined by AND/OR/WITH. Deliberately shape-only: the
# checker verifies that a license EXISTS and is well formed, not that an id is semantically right
# for the package (that is what the per-component evidence review decided).
_SPDX = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.+-]*'
                   r'(?:\s+(?:AND|OR|WITH)\s+[A-Za-z0-9][A-Za-z0-9.+-]*)*$')

#: Declared-license strings that name exactly one SPDX id, in the forms publishers use.
_DECLARED = {
    'mit': 'MIT',
    'mit license': 'MIT',
    'apache 2.0': 'Apache-2.0',
    'apache-2.0': 'Apache-2.0',
    'apache license 2.0': 'Apache-2.0',
    'mpl-2.0': 'MPL-2.0',
    '3-clause bsd license': 'BSD-3-Clause',
    'bsd-3-clause': 'BSD-3-Clause',
    'bsd-2-clause': 'BSD-2-Clause',
    'mit-0': 'MIT-0',
    'psf-2.0': 'PSF-2.0',
    'apache-2.0 or bsd-3-clause': 'Apache-2.0 OR BSD-3-Clause',
}


#: One SPDX licence identifier: no whitespace, therefore no AND/OR/WITH operator. CycloneDX 1.6
#: binds ``license.id`` to the packaged ``spdx.SNAPSHOT.schema.json`` identifier enumeration, so a
#: compound declaration written into ``id`` is invalid even though it matches the expression shape.
_SPDX_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9.+-]*')

#: Keys the CycloneDX 1.6 ``licenseChoice`` shapes allow (``additionalProperties: false``).
_ENTRY_KEYS = {'license', 'expression', 'acknowledgement', 'bom-ref'}
_LICENSE_KEYS = {'id', 'name', 'acknowledgement', 'bom-ref', 'text', 'url', 'licensing', 'properties'}


def licence_declaration(component: dict) -> tuple[str | None, str | None, str | None]:
    """Return ``(representation, declared text, reason)`` for one component's licence block.

    ``component.licenses`` is a CycloneDX ``licenseChoice`` and accepts exactly two shapes:

    * ``[{"license": {"id": <single SPDX identifier>, ...}}]`` -- the multiple-licenses form. Its
      ``id`` is bound to the SPDX identifier enumeration, so ``Apache-2.0 OR BSD-3-Clause`` can
      never live there (the defect CT-QC-047 corrected);
    * ``[{"expression": <SPDX expression>}]`` -- a tuple of exactly one expression, which is the
      only schema-legal representation of a compound declaration.

    ``reason`` is ``None`` when the block is well formed, a string when it is malformed, and the
    whole tuple is ``(None, None, None)`` when the component carries no licence block at all.
    """
    entries = component.get('licenses')
    if not entries:
        return None, None, None
    if not isinstance(entries, list):
        return None, None, f'licenses is {type(entries).__name__}, not a list'
    if len(entries) != 1:
        return None, None, f'{len(entries)} license entries (exactly one is required)'
    entry = entries[0]
    if not isinstance(entry, dict):
        return None, None, f'license entry is {type(entry).__name__}, not an object'
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        return None, None, f'unexpected key(s) {unknown} in the license entry'
    has_expression = 'expression' in entry
    has_license = 'license' in entry
    if has_expression and has_license:
        return None, None, 'the entry carries both expression and license'
    if has_expression:
        text = entry.get('expression')
        if not isinstance(text, str) or not _SPDX.fullmatch(text):
            return None, None, f'malformed SPDX expression {text!r}'
        return 'expression', text, None
    if not has_license:
        return None, None, 'the entry carries neither license nor expression'
    block = entry.get('license')
    if not isinstance(block, dict):
        return None, None, f'license is {type(block).__name__}, not an object'
    unknown = sorted(set(block) - _LICENSE_KEYS)
    if unknown:
        return None, None, f'unexpected key(s) {unknown} in the license object'
    text = block.get('id')
    if text is None:
        return None, None, 'the license block has no id (a named license is not used in this inventory)'
    if not _SPDX_ID.fullmatch(text):
        return None, None, (f'id {text!r} is not a single SPDX identifier; a compound '
                            f'declaration belongs in "expression"')
    return 'id', text, None


def canonical(name: str) -> str:
    """PEP 503 normalisation: SBOM uses dashes, packaging metadata may use underscores."""
    return re.sub(r'[-_.]+', '-', name).lower()


def parse_lock(path: Path) -> tuple[dict[str, str], dict[str, int]]:
    pins: dict[str, str] = {}
    hashes: dict[str, int] = {}
    current = None
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = _PIN.match(line)
        if match:
            current = canonical(match.group(1))
            pins[current] = match.group(2)
            hashes[current] = 0
            continue
        if current is not None:
            hashes[current] += len(_HASH.findall(line))
    return pins, hashes


def declared_from_metadata(metadata) -> tuple[str | None, str]:
    """Return (spdx_or_None, how) for one installed distribution's declared license."""
    expression = metadata.get('License-Expression')
    if expression:
        return expression.strip(), 'License-Expression'
    field = (metadata.get('License') or '').strip()
    if field:
        mapped = _DECLARED.get(field.lower())
        if mapped:
            return mapped, f'License={field!r}'
        return None, f'License={field!r} (not an SPDX id)'
    for classifier in metadata.get_all('Classifier') or []:
        if not classifier.startswith('License ::'):
            continue
        tail = classifier.split('::')[-1].strip()
        mapped = _DECLARED.get(tail.lower())
        if mapped:
            return mapped, f'Classifier={classifier!r}'
    return None, 'no declared license metadata'


def text_family(text: str) -> str | None:
    up = text.upper()
    if 'MIT NO ATTRIBUTION' in up:
        return 'MIT-0'
    if 'PERMISSION IS HEREBY GRANTED, FREE OF CHARGE' in up:
        return 'MIT'
    if 'APACHE LICENSE' in up and 'VERSION 2.0' in up:
        return 'Apache-2.0'
    if 'REDISTRIBUTION AND USE IN SOURCE AND BINARY FORMS' in up:
        return ('BSD-3-Clause' if ('NEITHER THE NAME' in up or 'ENDORSE OR PROMOTE' in up)
                else 'BSD-2-Clause')
    if 'MOZILLA PUBLIC LICENSE' in up:
        return 'MPL-2.0'
    if 'PYTHON SOFTWARE FOUNDATION LICENSE' in up:
        return 'PSF-2.0'
    return None


def vendored_family(dist) -> str | None:
    base = Path(dist._path)
    chunks = []
    for directory in [base] + ([base / 'licenses'] if (base / 'licenses').is_dir() else []):
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.name.upper().startswith(('LICENSE', 'COPYING')):
                chunks.append(entry.read_text(encoding='utf-8', errors='replace'))
    return text_family('\n'.join(chunks)) if chunks else None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--sbom', default=str(REPO / 'docs' / 'SBOM.runtime.cdx.json'))
    parser.add_argument('--lock', default=str(REPO / 'requirements.lock'))
    parser.add_argument('--installed', action='store_true',
                        help='also compare each block with the installed distribution metadata')
    args = parser.parse_args(argv)

    failures: list[str] = []
    pins, hashes = parse_lock(Path(args.lock))
    document = json.loads(Path(args.sbom).read_bytes().decode('utf-8'))
    components = document.get('components') or []

    print(f'lock pins:            {len(pins)}')
    print(f'sbom components:      {len(components)}')
    unpinned = sorted(name for name, count in hashes.items() if count == 0)
    print(f'release:              {document.get("specVersion")} ({document.get("bomFormat")})')
    total_hashes = sum(hashes.values())
    print(f'hash pins parsed:     {total_hashes} sha256 entries')
    if unpinned:
        failures.append(f'{len(unpinned)} lock entries carry no sha256 hash: {unpinned}')

    # 1. name/version coherence, both directions
    by_name: dict[str, dict] = {}
    duplicates = []
    for component in components:
        key = canonical(component.get('name', ''))
        if key in by_name:
            duplicates.append(key)
        by_name[key] = component
    if duplicates:
        failures.append(f'duplicate component names in the SBOM: {sorted(set(duplicates))}')
    missing_from_sbom = sorted(set(pins) - set(by_name))
    extra_in_sbom = sorted(set(by_name) - set(pins))
    version_mismatch = sorted(name for name in set(pins) & set(by_name)
                              if pins[name] != by_name[name].get('version'))
    print(f'names only in lock:   {missing_from_sbom or "none"}')
    print(f'names only in sbom:   {extra_in_sbom or "none"}')
    print(f'version mismatches:   {version_mismatch or "none"}')
    for label, values in (('missing from SBOM', missing_from_sbom),
                          ('absent from the lock', extra_in_sbom),
                          ('version disagrees with the lock', version_mismatch)):
        if values:
            failures.append(f'{len(values)} distribution(s) {label}: {values}')
    if len(pins) != len(components):
        failures.append(f'count mismatch: {len(pins)} lock pins vs {len(components)} SBOM components')

    # 2. every component carries exactly one well-formed declared license, in a representation the
    #    CycloneDX 1.6 ``licenseChoice`` schema accepts: an ``id`` entry must hold a single SPDX
    #    identifier, a compound declaration must use the one-element ``expression`` shape.
    unlicensed = []
    malformed = []
    representations: dict[str, int] = {}
    for key, component in sorted(by_name.items()):
        representation, text, reason = licence_declaration(component)
        if reason:
            malformed.append((component.get('name'), reason))
        elif representation is None:
            unlicensed.append(component.get('name'))
        else:
            representations[representation] = representations.get(representation, 0) + 1
    print(f'components with a license block: {len(by_name) - len(unlicensed)} / {len(by_name)}')
    print(f'license representations:         '
          f'{", ".join(f"{k}={v}" for k, v in sorted(representations.items())) or "none"}')
    if unlicensed:
        failures.append(f'{len(unlicensed)} component(s) have no license block: {unlicensed}')
    if malformed:
        failures.append(f'{len(malformed)} component(s) have a malformed license block: {malformed}')

    # 3. optional: the installed distribution must agree with the recorded block
    if args.installed:
        from importlib import metadata as importlib_metadata

        dists = {canonical(dist.metadata['Name']): dist
                 for dist in importlib_metadata.distributions() if dist.metadata['Name']}
        not_installed, disagree, unresolved = [], [], []
        for key, component in sorted(by_name.items()):
            dist = dists.get(key)
            if dist is None:
                not_installed.append(component.get('name'))
                continue
            if dist.version != component.get('version'):
                disagree.append((component.get('name'), component.get('version'), dist.version))
                continue
            declared, how = declared_from_metadata(dist.metadata)
            if declared is None:
                family = vendored_family(dist)
                if family is None:
                    unresolved.append((component.get('name'), how))
                    continue
                declared = family
            recorded = licence_declaration(component)[1]
            if recorded != declared:
                disagree.append((component.get('name'), recorded, declared))
        print(f'installed cross-check: {len(by_name) - len(not_installed) - len(disagree) - len(unresolved)}'
              f' / {len(by_name)} agree')
        if not_installed:
            failures.append(f'{len(not_installed)} component(s) not installed in this interpreter: '
                            f'{not_installed}; run under the lane .venv')
        if disagree:
            failures.append(f'{len(disagree)} component(s) disagree with installed metadata: {disagree}')
        if unresolved:
            failures.append(f'{len(unresolved)} component(s) have no resolvable installed '
                            f'license: {unresolved}')

    if failures:
        for failure in failures:
            print(f'FAIL: {failure}')
        print(f'RESULT: FAIL ({len(failures)} finding(s))')
        return 1
    print('RESULT: PASS (lock/SBOM names and versions agree, every component carries a license)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
