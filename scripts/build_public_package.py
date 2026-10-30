"""Build an explicit-allowlist, deterministic ZIP without copying Git history."""
import argparse,hashlib,json,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
BASE_MANIFEST='be8ce3af679bd3b2c75501ed80524ac66030fd7aa96c692718f7aa0322d30343'
def public_path(path):
    return path in json.loads((ROOT/'PUBLIC_FILES.json').read_text(encoding='utf-8'))
def build(output):
    output=Path(output).resolve()
    if output==ROOT or ROOT in output.parents:raise ValueError('Output must be outside the source tree.')
    if output.exists() or output.with_suffix('.identity.json').exists():raise ValueError('Refusing to overwrite a release.')
    allowed=json.loads((ROOT/'PUBLIC_FILES.json').read_text(encoding='utf-8'))
    if len(allowed)!=len(set(allowed)):raise ValueError('Duplicate allowlist entry.')
    payload={}
    for name in sorted(allowed):
        path=ROOT/name
        if Path(name).is_absolute() or '..' in Path(name).parts or not path.is_file() or not path.resolve().is_relative_to(ROOT):
            raise ValueError('Invalid allowlist member.')
        payload[name]=path.read_bytes()
    manifest={'schema':'civictrace-portable-release/1','version':'portable-rc3','accepted_base_manifest_sha256':BASE_MANIFEST,
              'members':{n:{'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)} for n,b in payload.items()}}
    payload['MANIFEST.json']=(json.dumps(manifest,indent=2,sort_keys=True)+'\n').encode()
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for n,b in sorted(payload.items()):
            info=zipfile.ZipInfo('CivicTrace/'+n,date_time=(1980,1,1,0,0,0));info.create_system=3;info.create_version=20;info.extract_version=20;info.flag_bits=0;info.internal_attr=0
            info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
            z.writestr(info,b)
    identity={'archive':output.name,'archive_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
              'manifest_sha256':hashlib.sha256(payload['MANIFEST.json']).hexdigest(),'files_excluding_manifest':len(manifest['members']),
              'accepted_base_manifest_sha256':BASE_MANIFEST,'version':'portable-rc3','git_history_included':False}
    output.with_suffix('.identity.json').write_text(json.dumps(identity,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(identity,indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('output',type=Path);build(p.parse_args().output)
