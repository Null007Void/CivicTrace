# Third-party notices

The CivicTrace runtime container image installs exactly the distributions pinned in
`requirements.lock` (`pip install --require-hashes`) and serves them to recipients of the
image. This file records who holds copyright in each of those components, under which
license it is redistributed, and where the complete license text travels with the image.

These attribution and license-text sections were generated during the accepted base
qualification from the pinned SBOM (`docs/SBOM.runtime.cdx.json`) and installed metadata
for those same versions. They are preserved here; the original internal generation script
is not part of this portable distribution. `scripts/check_sbom_coherence.py --installed`
checks installed versions and declared licenses against the SBOM/lock; it does not regenerate
or fully validate this notice. When changing dependencies, reassess and update notices from
the actual distribution license/NOTICE files as described in `docs/THIRD_PARTY.md`.
This file records attribution; it is not legal advice and asserts no legal clearance.

## Runtime components (34)

| Component | Version | SPDX license | Copyright notice (as shipped) | Upstream source |
|---|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | Copyright (c) 2025 Sebastián Ramírez | https://github.com/fastapi/annotated-doc |
| annotated-types | 0.8.0 | MIT | Copyright (c) 2022 the contributors | https://github.com/annotated-types/annotated-types |
| anyio | 4.15.1 | MIT | Copyright (c) 2018 Alex Grönholm | https://github.com/agronholm/anyio |
| certifi | 2026.10.19 | MPL-2.0 | - | https://github.com/certifi/python-certifi |
| cffi | 2.1.1 | MIT-0 | - | https://github.com/python-cffi/cffi |
| charset-normalizer | 3.5.2 | MIT | Copyright (c) 2025 TAHRI Ahmed R. | https://github.com/jawah/charset_normalizer |
| click | 8.5.0 | BSD-3-Clause | Copyright 2014 Pallets | https://github.com/pallets/click/ |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | Copyright (c) Individual contributors. | https://github.com/pyca/cryptography/ |
| fastapi | 0.161.0 | MIT | Copyright (c) 2018 Sebastián Ramírez | https://github.com/fastapi/fastapi |
| google-api-core | 2.43.1 | Apache-2.0 | - | https://github.com/googleapis/google-cloud-python |
| google-auth | 2.62.0 | Apache-2.0 | - | https://github.com/googleapis/google-cloud-python/tree/main/packages/google-auth |
| google-cloud-core | 2.8.0 | Apache-2.0 | - | https://github.com/googleapis/google-cloud-python/tree/main/packages/google-cloud-core |
| google-cloud-firestore | 2.34.1 | Apache-2.0 | - | https://github.com/googleapis/google-cloud-python/tree/main/packages/google-cloud-firestore |
| googleapis-common-protos | 1.76.0 | Apache-2.0 | - | https://github.com/googleapis/google-cloud-python/tree/main/packages/googleapis-common-protos |
| grpcio | 1.85.0 | Apache-2.0 | - | https://github.com/grpc/grpc |
| grpcio-status | 1.85.0 | Apache-2.0 | - | https://grpc.io |
| h11 | 0.16.0 | MIT | Copyright (c) 2016 Nathaniel J. Smith <njs@pobox.com> and other contributors | https://github.com/python-hyper/h11 |
| httpcore | 1.0.9 | BSD-3-Clause | Copyright © 2020, [Encode OSS Ltd](https://www.encode.io/). | https://github.com/encode/httpcore |
| httpx | 0.28.1 | BSD-3-Clause | Copyright © 2019, [Encode OSS Ltd](https://www.encode.io/). | https://github.com/encode/httpx |
| idna | 3.20 | BSD-3-Clause | Copyright (c) 2013-2026, Kim Davies and contributors. | https://github.com/kjd/idna |
| opentelemetry-api | 1.45.1 | Apache-2.0 | - | https://github.com/open-telemetry/opentelemetry-python |
| proto-plus | 1.29.0 | Apache-2.0 | - | https://github.com/googleapis/google-cloud-python |
| protobuf | 7.36.2 | BSD-3-Clause | Copyright 2008 Google Inc.  All rights reserved. | https://developers.google.com/protocol-buffers/ |
| pyasn1 | 0.6.5 | BSD-2-Clause | Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com> | https://github.com/pyasn1/pyasn1 |
| pyasn1-modules | 0.4.2 | BSD-2-Clause | Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com> | https://github.com/pyasn1/pyasn1-modules |
| pycparser | 3.12 | BSD-3-Clause | Copyright (c) 2008-2027, Eli Bendersky | https://github.com/eliben/pycparser |
| pydantic | 2.14.1 | MIT | Copyright (c) 2017 to present Pydantic Services Inc. and individual contributors. | https://github.com/pydantic/pydantic |
| pydantic-core | 2.50.1 | MIT | Copyright (c) 2022 Samuel Colvin | https://github.com/pydantic/pydantic/tree/main/pydantic-core |
| requests | 2.34.2 | Apache-2.0 | Copyright 2019 Kenneth Reitz | https://github.com/psf/requests |
| starlette | 1.9.0 | BSD-3-Clause | Copyright © 2018, [Encode OSS Ltd](https://www.encode.io/). | https://github.com/Kludex/starlette |
| typing-extensions | 4.16.0 | PSF-2.0 | Copyright (c) 1991 - 1995, Stichting Mathematisch Centrum Amsterdam, | https://github.com/python/typing_extensions |
| typing-inspection | 0.4.4 | MIT | Copyright (c) Pydantic Services Inc. 2025 to present | https://github.com/pydantic/typing-inspection |
| urllib3 | 2.8.0 | MIT | Copyright (c) 2008-2020 Andrey Petrov and contributors. | https://github.com/urllib3/urllib3 |
| uvicorn | 0.54.0 | BSD-3-Clause | Copyright © 2017-present, [Encode OSS Ltd](https://www.encode.io/). | https://github.com/Kludex/uvicorn |

Components marked `-` carry no copyright line: either the shipped text has none of its own
(Apache-2.0 boilerplate, the MPL-2.0 exhibit certifi uses, or MIT-0, which exists to require
none), or the shipped LICENSE mixes scopes and its copyright lines belong to other
components. Their attribution is the license text plus the notices reproduced below.

* no copyright line in the shipped text: `certifi`, `cffi`, `google-api-core`, `google-auth`, `google-cloud-core`, `google-cloud-firestore`, `googleapis-common-protos`, `opentelemetry-api`, `proto-plus`.
* LICENSE mixes license scopes, so its copyright lines are not this component's: `grpcio`, `grpcio-status`.

## Notices that must travel with the redistribution

### requests — Apache-2.0 NOTICE (reproduced verbatim)

Apache-2.0 section 4(d) requires the NOTICE file of a redistributed work to travel with it.
The file shipped by `requests` 2.34.2 says, in full:

```
Requests
Copyright 2019 Kenneth Reitz
```

### certifi — MPL-2.0

`certifi` 2026.10.19 is redistributed unmodified under the Mozilla Public License 2.0. The
license block shipped inside its own package metadata reads:

```
This package contains a modified version of ca-bundle.crt:

ca-bundle.crt -- Bundle of CA Root Certificates

This is a bundle of X.509 certificates of public Certificate Authorities
(CA). These were automatically extracted from Mozilla's root certificates
file (certdata.txt).  This file can be found in the mozilla source tree:
https://hg.mozilla.org/mozilla-central/file/tip/security/nss/lib/ckfw/builtins/certdata.txt
It contains the certificates in PEM format and therefore
can be directly used with curl / libcurl / php_curl, or with
an Apache+mod_ssl webserver for SSL client authentication.
Just configure this file as the SSLCACertificateFile.#

***** BEGIN LICENSE BLOCK *****
This Source Code Form is subject to the terms of the Mozilla Public License,
v. 2.0. If a copy of the MPL was not distributed with this file, You can obtain
one at http://mozilla.org/MPL/2.0/.

***** END LICENSE BLOCK *****
@(#) $RCSfile: certdata.txt,v $ $Revision: 1.80 $ $Date: 2011/11/03 15:11:58 $
```

CivicTrace modifies nothing in this package. The corresponding source is available from
https://github.com/certifi/python-certifi at tag `2026.10.19`, and the complete MPL-2.0 text ships in the image
at `site-packages/certifi-2026.10.19.dist-info/licenses/LICENSE` and its accompanying
`METADATA` (MPL-2.0 section 3.1 requires the source to be obtainable; section 3.2 covers
source form).

### grpcio — MPL-2.0 scoped to a bundled file

`grpcio` 1.85.0 as a distribution is Apache-2.0 (the id recorded in the SBOM), but its
shipped `LICENSE` scopes a second license to one bundled data file, which the wheel also
carries:

```
Following applies to:
./etc/roots.pem

Mozilla Public License Version 2.0
==================================
```

In the wheel that source path is installed as `grpc/_cython/_credentials/roots.pem`, so the
image redistributes an MPL-2.0 file. It is unmodified; the complete MPL-2.0 text ships at
`site-packages/grpcio-1.85.0.dist-info/licenses/LICENSE` and the source is available from
https://github.com/grpc/grpc. The same `LICENSE` also scopes
BSD-3-Clause to Objective-C podspec sources that are not part of the installed wheel.

## License texts reproduced

The short permissive texts are reproduced here, each quoted from one package that ships it,
because their terms name the notice that has to be included with copies. Where packages ship
the same license with a different copyright line, the unmodified file for that package is the
one in the image and the line is in the table above. Apache-2.0, MPL-2.0 and PSF-2.0 texts are
kilobytes long and are not duplicated here: the complete, unmodified files ship in the image
(next section), which is what section 4(a) of Apache-2.0 and section 3.1 of MPL-2.0 require.

### MIT (as shipped by annotated-doc 0.0.5)

```
The MIT License (MIT)

Copyright (c) 2025 Sebastián Ramírez

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

### BSD-2-Clause (as shipped by pyasn1 0.6.5)

```
Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

  * Redistributions of source code must retain the above copyright notice, 
    this list of conditions and the following disclaimer.

  * Redistributions in binary form must reproduce the above copyright notice,
    this list of conditions and the following disclaimer in the documentation
    and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE 
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE 
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE. 
```

### BSD-3-Clause (as shipped by click 8.5.0)

```
Copyright 2014 Pallets

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

1.  Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.

2.  Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in the
    documentation and/or other materials provided with the distribution.

3.  Neither the name of the copyright holder nor the names of its
    contributors may be used to endorse or promote products derived from
    this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### Apache-2.0 OR BSD-3-Clause (cryptography 50.0.2)

Both texts ship side by side and either may be relied on:

* `LICENSE` — 197 bytes
* `LICENSE.APACHE` — 11360 bytes
* `LICENSE.BSD` — 1532 bytes

## Where the complete license texts ship

Every component above ships its unmodified license file inside its own distribution
metadata in the image, at `site-packages/<dist>-<version>.dist-info/licenses/`. The runtime
image installs from `requirements.lock` with hashes verified, so those files are the same
bytes the upstream projects publish. Per-component file inventory:

| Component | License file(s) shipped in the image | sha256 |
|---|---|---|
| annotated-doc 0.0.5 | `LICENSE` | fff170779a6acbf6 |
| annotated-types 0.8.0 | `LICENSE` | fe1049884b1a0d93 |
| anyio 4.15.1 | `LICENSE` | 5361ac9dc58f2ef5 |
| certifi 2026.10.19 | `LICENSE` | e93716da6b9c0d5a |
| cffi 2.1.1 | `LICENSE` | 5ba24ddc57067f92 |
| charset-normalizer 3.5.2 | `LICENSE` | 18577485d3704f1a |
| click 8.5.0 | `LICENSE.txt` | 9a8ad106a394e853 |
| cryptography 50.0.2 | `LICENSE`, `LICENSE.APACHE`, `LICENSE.BSD` | 3e0c7c091a948b82, aac73b3148f6d1d7, 602c4c7482de6479 |
| fastapi 0.161.0 | `LICENSE` | 4ec89ffc81485b97 |
| google-api-core 2.43.1 | `LICENSE` | cfc7749b96f63bd3 |
| google-auth 2.62.0 | `LICENSE` | c71d239df91726fc |
| google-cloud-core 2.8.0 | `LICENSE` | cfc7749b96f63bd3 |
| google-cloud-firestore 2.34.1 | `LICENSE` | cfc7749b96f63bd3 |
| googleapis-common-protos 1.76.0 | `LICENSE` | cfc7749b96f63bd3 |
| grpcio 1.85.0 | `LICENSE` | ed9abc49f1fa3e3c |
| grpcio-status 1.85.0 | `LICENSE` | 58c86a3f2b5cca0e |
| h11 0.16.0 | `LICENSE.txt` | 37db5bb85926db28 |
| httpcore 1.0.9 | `LICENSE.md` | fdcb59154c74cbab |
| httpx 0.28.1 | `LICENSE.md` | 4ec59d544f12b5f5 |
| idna 3.20 | `LICENSE.md` | 1a9a4f0e3d479a27 |
| opentelemetry-api 1.45.1 | `LICENSE` | c71d239df91726fc |
| proto-plus 1.29.0 | `LICENSE` | cfc7749b96f63bd3 |
| protobuf 7.36.2 | `LICENSE` | 6e5e117324afd944 |
| pyasn1 0.6.5 | `LICENSE.rst` | 2aad5fc00f705c4a |
| pyasn1-modules 0.4.2 | `LICENSE.txt` | 2aad5fc00f705c4a |
| pycparser 3.12 | `LICENSE` | 0a9e78156ae8b96c |
| pydantic 2.14.1 | `LICENSE` | a9e186f3ca16b5ee |
| pydantic-core 2.50.1 | `LICENSE` | fbe7f615f18d135c |
| requests 2.34.2 | `LICENSE` | 09e8a9bcec806710 |
| starlette 1.9.0 | `LICENSE.md` | dcb95677a0224024 |
| typing-extensions 4.16.0 | `LICENSE` | 3b2f81fe21d181c4 |
| typing-inspection 0.4.4 | `LICENSE` | 804b59b25f2c31bd |
| urllib3 2.8.0 | `LICENSE.txt` | 130e3a64d5fdd5d0 |
| uvicorn 0.54.0 | `LICENSE.md` | efe1acf3e62fb99c |

## Scope and limitations

* This file covers the 34 runtime distributions in the pinned image. Development-only
  packages (`requirements-dev.lock`) are not redistributed and are inventoried separately in
  `docs/LICENSE_INVENTORY.md`.
* Container base-image packages (`python:3.13-slim`, Debian) are outside this inventory;
  base-image license inspection needs a Docker daemon and is recorded as open in
  `docs/THIRD_PARTY.md`.
* License identifiers here are the SPDX expressions each package declares for its exact
  pinned version. Where a package bundles a file under another license, that file is named
  explicitly above rather than folded into the component id.
