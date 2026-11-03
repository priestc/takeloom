import numpy as np
from shapely.geometry import Polygon
from shapely.prepared import prep
from collections import deque
def check(W,t,H,T,lb,lt,L=3.0):
    obs=prep(Polygon([(-50,-50),(0,-50),(0,H),(T,H),(T,H-lt+1.2),(T+1.2,H-lt),(T+L,H-lt),(T+L,H+L),(-50,H+L)]).union(
         Polygon([(-50,-50),(T+L,-50),(T+L,lb),(T+1.2,lb),(T,lb-1.2),(T,0),(-50,0)])).buffer(-0.05))
    def poly(cy,cz,th):
        th=np.radians(th); d=np.array([np.sin(th),-np.cos(th)]); n=np.array([np.cos(th),np.sin(th)])
        C=np.array([cy,cz]); return Polygon([C,C+t*n,C+t*n+W*d,C+W*d])
    ys=np.arange(0,70,0.5); zs=np.arange(W-45,H+0.01,0.25); ths=np.arange(0,80,1)
    start=(1,int(np.argmin(abs(zs-W))),0)
    if obs.intersects(poly(ys[0]+0.5,W,0)): return "no-fit"
    free=lambda i,j,k: not obs.intersects(poly(ys[i],zs[j],ths[k]))
    seen={start}; q=deque([start])
    while q:
      i,j,k=q.popleft()
      if poly(ys[i],zs[j],ths[k]).bounds[0]>T+L: return True
      for di,dj,dk in [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)]:
        s=(i+di,j+dj,k+dk)
        if 0<=s[0]<len(ys) and 0<=s[1]<len(zs) and 0<=s[2]<len(ths) and s not in seen and free(*s):
          seen.add(s); q.append(s)
    return False
if __name__=="__main__":
    lb=4.0; Wmax,Wmin=80.0,64.0
    H=Wmax+lb+1.5; lt=H-Wmin+2.5; T=15.0
    print(f"H={H} lt={lt} T={T} opening={H-lt-lb}")
    phones={"16 Pro Max":(77.6,8.25),"13 mini":(64.2,7.65),"S20 FE":(74.5,8.4)}
    for n,(W,t) in phones.items():
        for case,(dw,dt) in {"bare":(0,0),"slim case":(1.6,1.8),"thick case":(2.6,4.0)}.items():
            r=check(W+dw,t+dt,H,T,lb,lt)
            print(f"{n:11s} {case:10s} {W+dw:5.1f}x{t+dt:4.1f} in/out={r} top overlap={lt-(H-W-dw):.1f}mm")
