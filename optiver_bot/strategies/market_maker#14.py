#!/usr/bin/env python3
"""Hybrid #14: #10 persistent momentum targets + #13 jump-following signals.

One #10 runner/connection; #13 contributes signals only, never its order loop.
Default execution is dry-run. Zero fees and 25 calls/s are local assumptions,
not verified challenge rules. --no-priors disables #13's provisional pair map.
"""
from collections import deque
import importlib.util
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))


def load(number):
    name=f'_optiver_hybrid14_{number}'
    if name not in sys.modules:
        spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(f'market_maker#{number}.py'))
        module=importlib.util.module_from_spec(spec);sys.modules[name]=module
        try:spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name,None)
            raise
    return sys.modules[name]


target,jump=load(10),load(13)
Config,Snapshot,decide=target.Config,target.Snapshot,target.decide


class HybridController(target.Controller):
    """Bounded causal co-jump learning, with independent per-stock targets."""
    def __init__(self,*args,use_priors=True,**kwargs):
        super().__init__(*args,**kwargs)
        self.short={};self.links={k:dict(beta=v,n=0,src='prior') for k,v in jump.PRIORS.items()} if use_priors else {}
        self.last_trigger={};self.events=deque(maxlen=256)
        self.jump_support={};self.latest={}

    def direction(self,iid,history,now,c,tick):
        momentum=super().direction(iid,history,now,c,tick)
        mid=history[-1][1]
        old=self.latest.get(iid)
        if old and (now-old[0]<=0 or now-old[0]>c.max_gap):
            self.short.pop(iid,None);self.jump_support.pop(iid,None)
        self.latest[iid]=(now,mid,tick)
        h=self.short.setdefault(iid,deque())
        h.append((now,mid))
        while len(h)>1 and h[1][0]<=now-1.:h.popleft()
        move=(mid-h[0][1])/tick if len(h)>1 and now-h[0][0]<=1.+c.max_gap else 0.
        # Evaluate completed events only from observations at/after their expiry.
        while self.events and now>=self.events[0]['t']:
            event=self.events.popleft()
            for follower,(t0,p0,ftick) in event['at'].items():
                latest=self.latest.get(follower)
                if not latest or now-latest[0]>c.max_gap or latest[0]<event['t']-c.max_gap:continue
                change=latest[1]-p0
                if abs(change)/ftick<4:continue
                beta=jump.clip((change/p0)/event['ret'],-1.5,1.5)
                if abs(beta)<.15:continue
                key=(event['leader'],follower);link=self.links.get(key)
                if link is None:link=self.links[key]=dict(beta=beta,n=0,src='cojump')
                elif link['beta']*beta>0:link['beta']=(link['beta']+beta)/2
                else:link.update(beta=beta,n=-1,src='cojump')
                link['n']+=1
        if abs(move)>=5.-1e-9 and now-self.last_trigger.get(iid,-1e9)>=15.:
            self.last_trigger[iid]=now
            ret=(mid-h[0][1])/h[0][1]
            fresh={i:v for i,v in self.latest.items() if i!=iid and now-v[0]<=c.max_gap}
            self.events.append(dict(t=now+10.,leader=iid,ret=ret,at=fresh))
            for (leader,follower),link in self.links.items():
                if leader!=iid or (link['src']!='prior' and link['n']<2):continue
                if follower not in fresh:continue
                _,fmid,ftick=fresh[follower]
                fh=self.short.get(follower)
                if not fh:continue
                expected=link['beta']*ret*fmid/ftick
                done=(fmid-fh[0][1])/ftick
                remaining=expected-done
                # Costs plus a full configured spread must fit inside the proxy.
                hurdle=max(3.,(c.cost_ticks or 0)+c.max_spread_ticks+c.min_net_ticks)
                if remaining*expected>0 and abs(remaining)>=hurdle:
                    self.jump_support[follower]=(now+10.,1 if expected>0 else -1,fmid+remaining*ftick)
        support=self.jump_support.get(iid)
        hurdle=max(3.,(c.cost_ticks or 0)+c.max_spread_ticks+c.min_net_ticks)
        follower_direction=(support[1] if support and now<support[0]
                            and support[1]*(support[2]-mid)/tick>=hurdle else 0)
        if momentum and follower_direction and momentum!=follower_direction:return 0
        return follower_direction or momentum


def main(argv=None,exchange_factory=None):
    args=list(sys.argv[1:] if argv is None else argv)
    use_priors='--no-priors' not in args
    if not use_priors:args.remove('--no-priors')
    if args and args[0]=='run':
        defaults={'--cost-ticks':'0','--max-requests':'25','--max-order-size':'5',
                  '--session-seconds':'600','--instruments':'AMD,AVGO,CRWV,MU,NBIS,NVDA,SNDK,VRT'}
        for flag,value in defaults.items():
            if not any(a==flag or a.startswith(flag+'=') for a in args):args.extend([flag,value])
        print('HYBRID #14: confirmed momentum + provisional/learned jump links; '
              'zero-fee default, local 25-call/s ceiling. Verify event settings.')
    def factory(*a,**kw):return HybridController(*a,use_priors=use_priors,**kw)
    return target.main(args,exchange_factory=exchange_factory,controller_factory=factory)


if __name__=='__main__':raise SystemExit(main())
