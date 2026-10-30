"""Offline launcher tests. Test keys only; no server or network launched."""
import os
import pytest
import launch

def setup_run(monkeypatch,tmp_path):
    monkeypatch.setattr(launch.sys.stdin,'isatty',lambda:True)
    monkeypatch.setenv('CIVICTRACE_CASE_STORE','unwanted')
    monkeypatch.setenv('CIVICTRACE_DEPLOYMENT_ID','unwanted')
    monkeypatch.setenv('NEBIUS_API_KEY','INHERITED_SENTINEL')
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED','1')
    # Restore the whole environment after each launcher invocation, including its new entries.
    before=dict(os.environ)
    monkeypatch.setattr(launch.os,'environ',dict(before))
    return ['--data-dir',str(tmp_path/'data')]

def test_offline_default_ignores_inherited_live_key(monkeypatch,tmp_path):
    args=setup_run(monkeypatch,tmp_path)
    def no_prompt(*a,**k):raise AssertionError('Offline must not prompt')
    monkeypatch.setattr(launch.getpass,'getpass',no_prompt)
    seen=[]
    monkeypatch.setattr(launch.runpy,'run_module',lambda *a,**k:seen.append(dict(launch.os.environ)))
    launch.main(args)
    assert seen[0]['CIVICTRACE_LIVE_ENABLED']=='0' and 'NEBIUS_API_KEY' not in seen[0]
    assert seen[0]['CIVICTRACE_DEPLOYMENT_ID']=='civictrace-local-v1'
    assert seen[0]['CIVICTRACE_MAX_LIVE_ATTEMPTS']=='2'
    assert seen[0]['CIVICTRACE_MAX_LIVE_USD']=='0.90'
    assert not (tmp_path/'data'/'spend').exists()

def test_live_secret_only_during_foreground_run_and_ledger_preserved(monkeypatch,tmp_path,capsys):
    args=setup_run(monkeypatch,tmp_path)
    spend=tmp_path/'data'/'spend';spend.mkdir(parents=True)
    marker=spend/'existing-ledger.fixture';marker.write_text('preserve prior reservations')
    monkeypatch.setattr(launch.getpass,'getpass',lambda *a,**k:'TEST_ONLY_SENTINEL')
    def run(*a,**k):
        assert launch.os.environ['NEBIUS_API_KEY']=='TEST_ONLY_SENTINEL'
        assert launch.os.environ['CIVICTRACE_LIVE_ENABLED']=='1'
        assert launch.os.environ['CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST']=='0.15'
        raise KeyboardInterrupt()
    monkeypatch.setattr(launch.runpy,'run_module',run)
    with pytest.raises(KeyboardInterrupt):launch.main(args+['--live'])
    assert 'NEBIUS_API_KEY' not in launch.os.environ
    assert launch.os.environ['CIVICTRACE_LIVE_ENABLED']=='0'
    assert marker.read_text()=='preserve prior reservations'
    assert 'TEST_ONLY_SENTINEL' not in capsys.readouterr().out

@pytest.mark.parametrize('key',['',' bad','bad\nkey','"quoted"','Bearer abc','nonascii\u00e9'])
def test_invalid_key_never_starts(monkeypatch,tmp_path,key):
    args=setup_run(monkeypatch,tmp_path)
    monkeypatch.setattr(launch.getpass,'getpass',lambda *a,**k:key)
    monkeypatch.setattr(launch.runpy,'run_module',lambda *a,**k:pytest.fail('Must not launch'))
    with pytest.raises(SystemExit):launch.main(args+['--live'])
    assert 'NEBIUS_API_KEY' not in launch.os.environ

def test_noninteractive_live_fails_before_prompt(monkeypatch,tmp_path):
    args=setup_run(monkeypatch,tmp_path);monkeypatch.setattr(launch.sys.stdin,'isatty',lambda:False)
    monkeypatch.setattr(launch.getpass,'getpass',lambda *a,**k:pytest.fail('No prompt'))
    with pytest.raises(SystemExit):launch.main(args+['--live'])

@pytest.mark.parametrize('value',['NaN','Infinity','-1','0','10001'])
def test_bad_budget_rejected(value):
    with pytest.raises(SystemExit):launch.parser().parse_args(['--budget-usd',value])

def test_unsupported_model_rejected():
    with pytest.raises(SystemExit):launch.parser().parse_args(['--model','another-provider/model'])


def test_real_entrypoint_imports_and_selects_loopback(monkeypatch,tmp_path):
    args=setup_run(monkeypatch,tmp_path)
    import uvicorn
    seen=[]
    def server(app,**kwargs):
        from civictrace.app import app as application
        assert application is not None
        seen.append((app,kwargs))
    monkeypatch.setattr(uvicorn,'run',server)
    launch.main(args)
    assert seen==[('civictrace.app:app',{'host':'127.0.0.1','port':8765,'reload':False})]
