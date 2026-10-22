#!/usr/bin/env python3
"""Run bounded model smoke tests; use --live only with development credentials."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Allow OpenAI calls using a development-only key')
    parser.add_argument('--env-file',type=Path,help='Development dotenv file; read only model key/model, never service credentials')
    parser.add_argument('--model',help='Override the configured router model')
    parser.add_argument('--report',type=Path,help='Save synthetic queries and model decisions as JSON')
    args=parser.parse_args()
    model=args.model or os.environ.get('ROO_SKILL_SMOKE_MODEL')
    if args.live:
        from dotenv import dotenv_values
        values=dotenv_values(args.env_file,interpolate=False) if args.env_file else {}
        key=(os.environ.get('ROO_SMOKE_DEV_OPENAI_API_KEY') or
             (os.environ.get('OPENAI_API_KEY') if os.environ.get('OPENAI_API_KEY')!='test' else None) or
             values.get('OPENAI_API_KEY'))
        if not key:
            parser.error('Supply a development-only key via ROO_SMOKE_DEV_OPENAI_API_KEY, OPENAI_API_KEY, or --env-file')
        os.environ['ROO_SMOKE_DEV_OPENAI_API_KEY']=key
        os.environ['ROO_SKILL_SMOKE_LIVE']='1'
        model=model or values.get('ROUTER_MODEL')
    else:
        os.environ.pop('ROO_SKILL_SMOKE_LIVE',None)
    if model:os.environ['ROO_SKILL_SMOKE_MODEL']=model
    if args.report:os.environ['ROO_SKILL_SMOKE_REPORT']=str(args.report.resolve())
    # Operational settings are always synthetic, regardless of the parent shell.
    os.environ.update(SLACK_BOT_TOKEN='test',SLACK_SIGNING_SECRET='test',OPENAI_API_KEY='test',
                      ROO_API_KEY='test',MLAI_BACKEND_URL='https://backend.test',
                      FOUNDER_TOOLS_LINK_ORIGINS='https://mlai.au')
    sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
    os.chdir(ROOT/'src')
    import pytest
    class Results:
        def __init__(self):
            self.tests=[]
        def pytest_runtest_logreport(self, report):
            if report.when == 'call' or (report.when == 'setup' and report.outcome != 'passed'):
                self.tests.append({'test':report.nodeid,'outcome':report.outcome})
    results=Results()
    code=pytest.main([str(ROOT/'tests/roo/test_live_skill_smoke.py'),'-q','-s','-p','no:cacheprovider'],plugins=[results])
    if args.report:
        output=Path(os.environ['ROO_SKILL_SMOKE_REPORT'])
        data=json.loads(output.read_text()) if output.exists() else {}
        data.update(exit_code=int(code),tests=results.tests)
        output.write_text(json.dumps(data,indent=2)+'\n')
    return code

if __name__=='__main__':
    raise SystemExit(main())
