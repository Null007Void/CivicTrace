"""Verify every archive member against its manifest and the external release identity."""
import argparse,hashlib,json,zipfile
from pathlib import Path
def verify(archive,identity):
    a=Path(archive); expected=json.loads(Path(identity).read_text(encoding='utf-8'))
    assert hashlib.sha256(a.read_bytes()).hexdigest()==expected['archive_sha256'],'Archive identity mismatch'
    with zipfile.ZipFile(a) as z:
        raw=z.read('CivicTrace/MANIFEST.json')
        assert hashlib.sha256(raw).hexdigest()==expected['manifest_sha256'],'Manifest mismatch'
        m=json.loads(raw); names=['CivicTrace/'+n for n in m['members']]+['CivicTrace/MANIFEST.json']
        assert sorted(z.namelist())==sorted(names),'Unexpected or duplicate member'
        for n,v in m['members'].items():
            assert not Path(n).is_absolute() and '..' not in Path(n).parts,'Unsafe member'
            data=z.read('CivicTrace/'+n)
            assert len(data)==v['bytes'] and hashlib.sha256(data).hexdigest()==v['sha256'],n
    print('PASS: archive, strict membership, file hashes and external identity')
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('archive');p.add_argument('identity');args=p.parse_args();verify(args.archive,args.identity)
