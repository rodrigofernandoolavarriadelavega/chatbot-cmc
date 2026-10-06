"""google_rating: si Google falla no se reintenta en cada visita y se conserva la última nota buena (6-oct-2026)."""
import sys,asyncio,time,tempfile,os; sys.path.insert(0,"app")
import session; session.DB_PATH=os.path.join(tempfile.mkdtemp(),"t.db")
import google_rating as g, httpx
g.API_KEY="x"; calls=[0]
class R:
    def __init__(s,code,j=None): s.status_code=code; s._j=j; s.text="err"
    def json(s): return s._j
class C:
    def __init__(s,*a,**k): pass
    async def __aenter__(s): return s
    async def __aexit__(s,*a): pass
    async def get(s,*a,**k): calls[0]+=1; return R(*RESP)
httpx.AsyncClient=C
RESP=(200,{"rating":4.8,"userRatingCount":200,"reviews":[]})
d=asyncio.run(g.fetch_rating()); assert d["rating"]==4.8 and calls[0]==1
g._CACHE.update(data=None,fetched_at=0.0); RESP=(429,None)
d=asyncio.run(g.fetch_rating()); assert d["rating"]==4.8, d   # disco
for _ in range(20): asyncio.run(g.fetch_rating())
assert calls[0]==2, calls      # no reintenta en cada visita
assert g.cached_rating()["rating"]==4.8
print("0 fallas")
