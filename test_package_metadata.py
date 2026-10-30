"""ZIP creator-system defaults must not vary package metadata; no browser/network."""
import json,zipfile
from scripts import build_public_package as builder
from scripts.verify_public_package import verify

def test_canonical_zip_metadata_across_simulated_platform_defaults(tmp_path,monkeypatch):
    root=tmp_path/'source';root.mkdir()
    (root/'README.md').write_text('Fixture content, not a product release.',encoding='utf-8')
    (root/'PUBLIC_FILES.json').write_text(json.dumps(['README.md','PUBLIC_FILES.json']),encoding='utf-8')
    monkeypatch.setattr(builder,'ROOT',root)
    real_info=zipfile.ZipInfo
    outputs=[]
    for platform_default in (0,3):
        class PlatformInfo(real_info):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs)
                self.create_system=platform_default
        monkeypatch.setattr(zipfile,'ZipInfo',PlatformInfo)
        output=tmp_path/('platform-'+str(platform_default)+'.zip')
        builder.build(output);outputs.append(output.read_bytes())
        with zipfile.ZipFile(output) as archive:
            for item in archive.infolist():
                assert item.create_system==3
                assert item.create_version==item.extract_version==20
                assert item.date_time==(1980,1,1,0,0,0)
                assert item.external_attr==0o100644<<16
                assert item.internal_attr==0
        verify(output,output.with_suffix('.identity.json'))
    assert outputs[0]==outputs[1]
