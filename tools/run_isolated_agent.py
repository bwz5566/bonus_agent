"""Run a bounded isolated agent attempt. No answers or approval bypasses."""
import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from bonus_agent.agent import run_agent
from bonus_agent.llm import LLM, load_config
from bonus_agent.recovery import RunBudget


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--materials',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--model',default='qwen-a')
    p.add_argument('--period',default='2026-09')
    p.add_argument('--max-steps',type=int,default=80)
    p.add_argument('--max-tokens',type=int,default=1000000)
    p.add_argument('--max-minutes',type=float,default=15)
    p.add_argument('--resume',action='store_true')
    a=p.parse_args()
    from tools.prepare_september_regression import verify
    if (a.materials.parent/'baseline_manifest.json').exists():
        verify(a.materials.parent)
    if a.materials.resolve()==a.out.resolve() or a.materials.resolve() in a.out.resolve().parents:
        raise ValueError('Output must be separate from input materials')
    if a.out.exists() and not a.resume:
        raise ValueError('Existing attempt requires explicit --resume')
    cfg=load_config()
    llm=LLM.from_config(a.model,cfg)
    if llm.api_key in ('','EMPTY'):
        raise ValueError('Local model credentials are missing; configure LOCAL_QWEN_API_KEY or providers.local_qwen.api_key')
    # This run is authorized only for the named intranet service, including
    # auxiliary routes. Never silently send a checker request elsewhere.
    todo=[llm];seen=set()
    while todo:
        route=todo.pop()
        if id(route) in seen:continue
        seen.add(id(route))
        url=urlsplit(route.base_url)
        if url.hostname!='192.168.81.98' or url.port!=9705:
            raise ValueError('Model route is outside the explicitly authorized endpoint')
        for name in ('checker_llm','vision_llm','think_llm'):
            other=getattr(route,name,None)
            if other is not None:todo.append(other)
    llm.timeout=120
    llm.think_timeout=180
    a.out.mkdir(parents=True,exist_ok=True)
    started=time.monotonic()
    def progress(i,s):
        print(json.dumps({'step':i,'tool':s.tool,'ok':s.ok,'result':str(s.result)[:500]},ensure_ascii=False),flush=True)
    try:
        ar=run_agent(llm,[a.materials],a.out,resume=a.resume,period=a.period,
            max_steps=a.max_steps,max_tokens=a.max_tokens,on_step=progress,
            run_budget=RunBudget(0,a.max_tokens,a.max_minutes*60))
        summary={'status':ar.status,'error':ar.error,'steps':len(ar.steps),
                 'questions':[q.text for q in ar.ws.pending] if ar.ws else []}
    except Exception as exc:
        summary={'status':'ERROR','error':str(exc)}
    summary.update({'model':a.model,'seconds':round(time.monotonic()-started,2),'usage':asdict(llm.usage)})
    (a.out/'attempt_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
