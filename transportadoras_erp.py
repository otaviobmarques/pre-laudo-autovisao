"""Entrada de Transportadoras como vistoriadores; unidades e acessos são locais."""
import json,threading,time,uuid
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
from clientes_erp import ACCESS,has_access,load_access,local_time
from consultar_transportadoras_erpflex import fetch_carriers
from transportadoras_regras import is_active
from parametros import normalize
LOCK=threading.Lock()
def init(con):
 con.execute('CREATE TABLE IF NOT EXISTS erp_transportadoras(erp_id TEXT PRIMARY KEY,local_id TEXT NOT NULL UNIQUE,data_json TEXT NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_transportadoras_estado(id INTEGER PRIMARY KEY CHECK(id=1),data_json TEXT NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_transportadoras_lease(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT NOT NULL,expires REAL NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_transportadoras_tentativas(dia TEXT PRIMARY KEY,quantidade INTEGER NOT NULL)')
def snapshot(con):
 if not con.execute("SELECT 1 FROM sqlite_master WHERE name='erp_transportadoras_estado'").fetchone():return None
 row=con.execute('SELECT data_json FROM erp_transportadoras_estado WHERE id=1').fetchone();return json.loads(row[0]) if row else None
def cached(con):
 if not (snapshot(con) or {}).get('ultima_atualizacao'):return None
 return [json.loads(r[0]) for r in con.execute('SELECT data_json FROM erp_transportadoras ORDER BY local_id')]
def map_carriers(rows,baseline,previous):
 if not rows:raise ValueError('Cadastro vazio; última base preservada.')
 ids=[str(r.get('id') or '').strip() for r in rows]
 if not all(ids) or len(set(ids))!=len(ids):raise ValueError('IDs ausentes ou duplicados; última base preservada.')
 if any(not str(r.get('descricao') or '').strip() for r in rows):raise ValueError('Transportadora sem nome.')
 active_names=Counter(normalize(r['descricao']) for r in rows if is_active(r))
 if any(n>1 for n in active_names.values()):raise ValueError('Nomes de vistoriadores ativos duplicados no ERP. Confira a marcação INATIVO no Complemento.')
 grouped=defaultdict(list)
 for r in rows:grouped[normalize(r['descricao'])].append(r)
 preferred={}
 for name,items in grouped.items():
  active=[r for r in items if is_active(r)]
  if len(active)==1:preferred[name]=str(active[0]['id'])
  elif len(items)==1:preferred[name]=str(items[0]['id'])
 old_by_erp={str(r['erp_id']):r for r in previous if r.get('erp_id')}
 already_bound={r['id'] for r in previous if r.get('erp_id')}
 base_groups=defaultdict(list)
 for r in baseline:base_groups[normalize(r['nome'])].append(r)
 result=[];used=set();linked=0
 for r,sid in zip(rows,ids):
  name=str(r['descricao']).strip();key=normalize(name);old=old_by_erp.get(sid)
  if old is None and preferred.get(key)==sid and len(base_groups.get(key,[]))==1:
   candidate=base_groups[key][0]
   if candidate['id'] not in already_bound:old=candidate
  local=old['id'] if old else 'ERPVIS_'+sid
  if local in used:raise ValueError('Conflito de identidade; última base preservada.')
  used.add(local);linked+=bool(old and local.startswith('LOCAL-'))
  result.append({'id':local,'erp_id':sid,'nome':name,'fantasia':str(r.get('fantasia') or '').strip(),'ativo_erp':is_active(r),'historical_only':False,'origem':'ERPFlex Transportadoras','situacao_origem':'Complemento INATIVO','data_alteracao_erp':r.get('dt_alteracao')})
 for old in [*previous,*baseline]:
  if old['id'] not in used:result.append({**old,'ativo_erp':False,'historical_only':True});used.add(old['id'])
 return result,{'total_erp':len(rows),'ativos':sum(is_active(r) for r in rows),'inativos':sum(not is_active(r) for r in rows),'vinculos_locais_preservados':linked,'unidade_origem_confirmada':False}
def apply(con,rows,baseline,requests=0):
 init(con);items,counts=map_carriers(rows,baseline,cached(con) or [])
 con.execute('DELETE FROM erp_transportadoras')
 for item in items:con.execute('INSERT INTO erp_transportadoras VALUES(?,?,?)',(str(item.get('erp_id') or 'HIST:'+item['id']),item['id'],json.dumps(item,ensure_ascii=False)))
 state={**counts,'ultima_atualizacao':datetime.now(timezone.utc).isoformat(),'erro':None,'requisicoes_ultima_carga':requests,'source':'ERPFlex /api/transportadoras','regra_situacao':'Complemento igual a INATIVO, ignorando caixa e espaços nas pontas'}
 con.execute('INSERT OR REPLACE INTO erp_transportadoras_estado VALUES(1,?)',(json.dumps(state,ensure_ascii=False),));return state

def daily_due(state,now=None):
 current=local_time(now);due=current.replace(hour=3,minute=30,second=0,microsecond=0)
 if current<due:due-=timedelta(days=1)
 previous=(state or {}).get('ultima_atualizacao')
 return not previous or datetime.fromisoformat(previous).astimezone(due.tzinfo)<due

class Sync:
 def __init__(self,db_factory,baseline,config):
  self.db=db_factory;self.baseline=baseline;self.config=config;self.running=False;self.error=None;self.stop_event=threading.Event();self.started=False
 def status(self):
  with self.db() as con:state=snapshot(con) or {}
  return {**state,'configurado':has_access(),'em_execucao':self.running,'ultimo_erro':self.error or state.get('erro'),'automatico':has_access(),'frequencia':'Diariamente às 03:30 (horário de Brasília)','limite_tentativas_diarias':1}
 def synchronize(self,credentials=None,preloaded=None):
  if not LOCK.acquire(blocking=False):raise ValueError('Uma atualização de transportadoras já está em execução.')
  owner=uuid.uuid4().hex;leased=False;self.running=True
  try:
   with self.db() as con:
    con.execute('BEGIN IMMEDIATE');init(con)
    old=con.execute('SELECT owner,expires FROM erp_transportadoras_lease WHERE id=1').fetchone()
    if old and old[1]>time.time():raise ValueError('Uma atualização de transportadoras já está em execução.')
    con.execute('INSERT OR REPLACE INTO erp_transportadoras_lease VALUES(1,?,?)',(owner,time.time()+1800));leased=True
   if preloaded is None:
    access=credentials or load_access();rows,requests=fetch_carriers(access['username'],access['password'],lambda message:None)
   else:rows,requests=preloaded
   baseline=json.loads(self.baseline.read_text(encoding='utf-8-sig'))['vistoriadores']
   with self.db() as con:
    con.execute('BEGIN IMMEDIATE');state=apply(con,rows,baseline,requests)
   self.error=None;return state
  except Exception:
   self.error='Não foi possível atualizar Transportadoras/vistoriadores. A última base válida foi mantida. Confira o acesso ao ERP, a rede e possíveis nomes ativos duplicados.'
   if leased:
    with self.db() as con:
     state=snapshot(con) or {};state['erro']=self.error
     con.execute('INSERT OR REPLACE INTO erp_transportadoras_estado VALUES(1,?)',(json.dumps(state,ensure_ascii=False),))
   raise
  finally:
   if leased:
    with self.db() as con:con.execute('DELETE FROM erp_transportadoras_lease WHERE id=1 AND owner=?',(owner,))
   self.running=False;LOCK.release()
 def run_daily(self):
  if not has_access():return False
  with self.db() as con:
   con.execute('BEGIN IMMEDIATE');init(con)
   if not daily_due(snapshot(con)):return False
   day=local_time().date().isoformat();row=con.execute('SELECT quantidade FROM erp_transportadoras_tentativas WHERE dia=?',(day,)).fetchone()
   if row and row[0]>=1:return False
   con.execute('INSERT INTO erp_transportadoras_tentativas VALUES(?,1) ON CONFLICT(dia) DO UPDATE SET quantidade=quantidade+1',(day,))
  self.synchronize();return True
 def trigger(self):
  if self.running:return False
  def task():
   try:self.synchronize()
   except Exception:pass
  threading.Thread(target=task,daemon=True).start();return True
 def start(self):
  if self.started:return
  self.started=True
  def loop():
   while not self.stop_event.wait(300):
    try:self.run_daily()
    except Exception:pass
  threading.Thread(target=loop,daemon=True).start()
