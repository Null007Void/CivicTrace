"""Portable, foreground local launcher. No .env loading and no stored credentials."""
from pathlib import Path
import argparse
from decimal import Decimal, InvalidOperation
import getpass
import os
import runpy
import sys
import warnings

MODELS=('nvidia/nemotron-3-super-120b-a12b','nvidia/Nemotron-3_5-Lightning')
DEPLOYMENT='civictrace-local-v1'

def default_data_dir():
    if os.name=='nt':
        base=Path(os.environ.get('LOCALAPPDATA') or (Path.home()/'AppData'/'Local'))
        return base/'CivicTrace'
    return Path.home()/'.local'/'share'/'civictrace'

def money(value):
    try:
        amount=Decimal(value)
        if not amount.is_finite() or not Decimal('0')<amount<=Decimal('10000'):
            raise ValueError()
        return format(amount,'f')
    except (InvalidOperation,ValueError):
        raise argparse.ArgumentTypeError('Use a positive finite dollar amount, at most 10000.') from None

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live',action='store_true',help='Enable paid Nebius calls after a hidden key prompt.')
    p.add_argument('--data-dir',type=Path,default=None,help='Persistent writable data directory. Reuse this on restart.')
    p.add_argument('--model',choices=MODELS,default=MODELS[0])
    p.add_argument('--max-investigations',type=int,default=2,help='Total lifetime claim attempts for this data directory (default 2).')
    p.add_argument('--budget-usd',type=money,default='0.90',help='Total estimated reservation ceiling, not an invoice cap (default 0.90).')
    return p

def configure(args):
    if not 1<=args.max_investigations<=500:
        raise SystemExit('--max-investigations must be between 1 and 500.')
    location=(args.data_dir or default_data_dir()).expanduser().resolve()
    location.mkdir(parents=True,exist_ok=True)
    # Do not inherit an old credential, live gate, alternate store or deployment identity.
    for name in list(os.environ):
        if name.startswith(('CIVICTRACE_','NEBIUS_')):
            os.environ.pop(name,None)
    os.environ.update(CIVICTRACE_LIVE_ENABLED='0',CIVICTRACE_CASE_STORE=str(location/'cases.json'),
        CIVICTRACE_SPEND_BACKEND='sqlite',CIVICTRACE_SPEND_STORE_PATH=str(location/'spend'),
        CIVICTRACE_SPEND_STORE_EXPECT_ROOT=str(location/'spend'),CIVICTRACE_DEPLOYMENT_ID=DEPLOYMENT,
        CIVICTRACE_MAX_LIVE_ATTEMPTS=str(args.max_investigations),CIVICTRACE_MAX_LIVE_USD=args.budget_usd,
        CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST='0.15',NEBIUS_MODEL=args.model)
    return location

def main(argv=None):
    args=parser().parse_args(argv)
    location=configure(args)
    print('Case data:',location/'cases.json',flush=True)
    print('Open http://127.0.0.1:8765/workbench . Stop with Ctrl+C; reuse this data directory on restart.',flush=True)
    try:
        if args.live:
            if not sys.stdin.isatty():raise SystemExit('Live mode requires a private interactive console; piped keys are refused.')
            print('Paid Nebius mode:',args.model,flush=True)
            print('Up to 3 provider requests per claim; $0.45 reserved per investigation. Total limits:',
                  args.max_investigations,'investigations / $'+args.budget_usd,flush=True)
            print('Existing reservations remain consumed. This is not a provider billing guarantee.',flush=True)
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                key=getpass.getpass('Nebius API key (hidden; keep recording off): ')
            if not key or key!=key.strip() or any(ord(c)<33 or ord(c)>126 for c in key) or key.lower().startswith('bearer ') or key[0] in "\"'":
                raise SystemExit('Invalid key format. Enter only the key without quotes or a Bearer prefix. Nothing sent.')
            (location/'spend').mkdir(exist_ok=True)
            os.environ['NEBIUS_API_KEY']=key
            del key
            os.environ['CIVICTRACE_LIVE_ENABLED']='1'
        else:
            print('Offline mode: no provider key or paid inference.',flush=True)
        # Import from this download regardless of the caller's working directory.
        root=Path(__file__).resolve().parent
        sys.path.insert(0,str(root))
        runpy.run_module('civictrace',run_name='__main__',alter_sys=True)
    finally:
        os.environ.pop('NEBIUS_API_KEY',None)
        os.environ['CIVICTRACE_LIVE_ENABLED']='0'

if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:print('CivicTrace stopped. Case data and spending reservations are preserved.')
