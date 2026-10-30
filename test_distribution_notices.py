"""Static distribution contract; does not imply a Docker runtime qualification."""
from pathlib import Path
import json

ROOT=Path(__file__).resolve().parents[1]

def test_container_carries_root_license_notices_and_runtime_inventory():
    docker=(ROOT/'Dockerfile').read_text(encoding='utf-8')
    license_copy='COPY LICENSE THIRD_PARTY_NOTICES.md ./'
    sbom_copy='COPY docs/SBOM.runtime.cdx.json ./docs/SBOM.runtime.cdx.json'
    assert license_copy in docker and sbom_copy in docker
    assert docker.index(license_copy)<docker.index('USER civic')
    assert docker.index(sbom_copy)<docker.index('USER civic')
    license_text=(ROOT/'LICENSE').read_text(encoding='utf-8')
    assert license_text.startswith('MIT License')
    assert 'Copyright (c) 2026 Null007Void / Null Foundry' in license_text
    assert 'Permission is hereby granted, free of charge' in license_text
    assert 'THE SOFTWARE IS PROVIDED' in license_text
    assert '## Runtime components (34)' in (ROOT/'THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8')
    assert len(json.loads((ROOT/'docs/SBOM.runtime.cdx.json').read_text())['components'])==34
    ignores=(ROOT/'.dockerignore').read_text(encoding='utf-8').splitlines()
    assert not any(n in ignores for n in ('LICENSE','THIRD_PARTY_NOTICES.md','docs','docs/SBOM.runtime.cdx.json'))

def test_public_inventory_retains_license_and_notices():
    allowed=json.loads((ROOT/'PUBLIC_FILES.json').read_text(encoding='utf-8'))
    assert {'LICENSE','THIRD_PARTY_NOTICES.md','docs/SBOM.runtime.cdx.json'}<=set(allowed)
