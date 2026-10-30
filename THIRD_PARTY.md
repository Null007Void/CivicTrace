# Dependency attribution and maintenance

CivicTrace code is MIT licensed. Runtime dependencies are fixed by `requirements.lock` and inventoried in `SBOM.runtime.cdx.json`; development packages appear in `LICENSE_INVENTORY.md`. `../THIRD_PARTY_NOTICES.md` retains the accepted base's component copyright statements, Apache NOTICE, bundled-file/MPL scope, and reproduced license texts. Those sections were not altered for RC2. The internal original notice-generation script is not shipped, so this package does not claim a runnable notice regeneration command.

For a dependency change: install the intended versions in an isolated environment; inspect each installed distribution's license and NOTICE files and metadata; update the lock, SBOM and inventory; preserve required notices, copyright and license texts (including bundled files); update THIRD_PARTY_NOTICES with traceable source references. Do not infer license permission from a package name or copy only an SPDX label when full notices are required. Run:

```text
<venv-python> scripts/check_sbom_coherence.py --installed
```

This verifies names, versions and declared-license coherence, not the completeness of all redistribution obligations. Review the updated notice text separately. No dependencies changed between RC1 and RC2.

The Python/Debian container base image has not received license or vulnerability inspection in this build because the Docker Linux engine is unavailable; container qualification remains open. The dependency inventory does not cover all operating-system packages in that image. Bundled tutorial municipal excerpts have separate attribution/provenance in `../tutorial/README.md`; they are not relicensed as application code or claimed to be authenticated originals.
