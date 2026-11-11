"""Entrada de Clientes ERPFlex: catálogo atômico, identidade estável e consulta periódica."""
import json,re,unicodedata,threading,time,ctypes,os,uuid
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
from consultar_clientes_erpflex import fetch_clients
ROOT=Path(__file__).resolve().parent
ACCESS=ROOT/'work'/'erpflex_clientes'/'acesso.dpapi'
STATUSES={'1':'Aprovado','0':'Aguardando Aprovação','2':'Inativo'}
LOCK=threading.Lock()

def atomic_json(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(path)

def protect(data,decrypt=False):
 if os.name!='nt':raise ValueError('Armazenamento de acesso requer o Windows.')
 from ctypes import wintypes
 class Blob(ctypes.Structure):_fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
 buf=ctypes.create_string_buffer(data);source=Blob(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)));dest=Blob()
 crypt=ctypes.WinDLL('crypt32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
 kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
 if decrypt:
  function=crypt.CryptUnprotectData;function.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
  ok=function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(dest))
 else:
  function=crypt.CryptProtectData;function.argtypes=[ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
  ok=function(ctypes.byref(source),'PreLaudo Clientes ERPFlex',None,None,None,1,ctypes.byref(dest))
 if not ok:raise ValueError('Não foi possível acessar a credencial protegida deste usuário do Windows.')
 try:return ctypes.string_at(dest.data,dest.size)
 finally:kernel.LocalFree(dest.data)

def save_access(username,password):
 ACCESS.parent.mkdir(parents=True,exist_ok=True);blob=protect(json.dumps({'username':username,'password':password},ensure_ascii=False).encode());temp=ACCESS.with_suffix('.tmp');temp.write_bytes(blob);temp.replace(ACCESS)

def env_access():
 """Acesso do ERP por variáveis de ambiente (hospedagem): ERPFLEX_USERNAME e ERPFLEX_PASSWORD."""
 username=(os.environ.get('ERPFLEX_USERNAME') or '').strip();password=os.environ.get('ERPFLEX_PASSWORD') or ''
 return {'username':username,'password':password} if username and password else None

# O servidor registra aqui a leitura do acesso cadastrado no Painel Administrativo.
PANEL_ACCESS=None
def panel_access():
 try:value=PANEL_ACCESS() if PANEL_ACCESS else None
 except Exception:value=None
 return value if value and value.get('username') and value.get('password') else None

def has_access():
 """Há acesso configurado: painel administrativo, variáveis de ambiente ou a credencial protegida do Windows."""
 return bool(panel_access() or env_access()) or ACCESS.exists()

def access_source():
 return 'painel' if panel_access() else 'ambiente' if env_access() else 'windows' if ACCESS.exists() else None

def load_access():return panel_access() or env_access() or json.loads(protect(ACCESS.read_bytes(),True).decode())

def init(con):
 con.execute('CREATE TABLE IF NOT EXISTS erp_clientes(erp_id TEXT PRIMARY KEY,local_id TEXT NOT NULL UNIQUE,data_json TEXT NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_clientes_estado(id INTEGER PRIMARY KEY CHECK(id=1),data_json TEXT NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_clientes_lease(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT NOT NULL,expires REAL NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_clientes_tentativas(dia TEXT PRIMARY KEY,quantidade INTEGER NOT NULL)')

def snapshot(con):
 if not con.execute("SELECT 1 FROM sqlite_master WHERE name='erp_clientes_estado'").fetchone():return None
 row=con.execute('SELECT data_json FROM erp_clientes_estado WHERE id=1').fetchone();return json.loads(row[0]) if row else None

def cached(con):
 if not (snapshot(con) or {}).get('ultima_atualizacao'):return None
 return [json.loads(r[0]) for r in con.execute('SELECT data_json FROM erp_clientes ORDER BY local_id')]

def _doc(v):return re.sub(r'\D','',str(v or ''))
def _text(v):return ' '.join(unicodedata.normalize('NFKC',str(v or '')).casefold().split())

def map_clients(rows,baseline,previous):
 if not rows:raise ValueError('O ERP retornou cadastro vazio. A última base foi preservada.')
 ids=[str(x.get('id') or '').strip() for x in rows]
 if any(not x for x in ids) or len(set(ids))!=len(ids):raise ValueError('Identificadores ausentes ou duplicados no cadastro de Clientes.')
 prior={str(c['erp_id']):c for c in previous if c.get('erp_id')}
 doc_index=defaultdict(list);names=defaultdict(list)
 for c in baseline:
  d=_doc(c.get('cpf_cnpj'))
  if d:doc_index[d].append(c)
  else:names[(_text(c.get('name')),_text(c.get('fantasy')))].append(c)
 docs=Counter(_doc(r.get('cpf_cnpj')) for r in rows);namecounts=Counter((_text(r.get('nome')),_text(r.get('fantasia'))) for r in rows)
 result=[];used=set();linked=0
 for row in rows:
  erp_id=str(row['id']);status=str(row.get('sit_cadastro',''));name=str(row.get('nome') or '').strip()
  if status not in STATUSES or not name:raise ValueError('Cliente com situação desconhecida ou sem razão social. Atualização não aplicada.')
  old=prior.get(erp_id);d=_doc(row.get('cpf_cnpj'));method='erp_id' if old else None
  if not old and d and docs[d]==1 and len(doc_index[d])==1:old=doc_index[d][0];method='cpf_cnpj_unico'
  if not old and not d:
   key=(_text(row.get('nome')),_text(row.get('fantasia')))
   if namecounts[key]==1 and len(names[key])==1:old=names[key][0];method='nome_e_fantasia_exatos_unicos_sem_documento'
  local=old['id_interno'] if old else 'ERPCLI_'+erp_id
  if local in used:raise ValueError('Conflito de identidade entre clientes; atualização não aplicada.')
  used.add(local);linked+=bool(old)
  period=str(row.get('adicional_1') or '').strip();period_ok=period in ('A Vista','Mensal','Quinzenal')
  try:table=int(row.get('tabela') or 0)
  except (TypeError,ValueError):raise ValueError('Vínculo de tabela de preços inválido; atualização não aplicada.') from None
  if table<0:raise ValueError('Vínculo de tabela de preços inválido.')
  pays=row.get('forma_pag') or {};pays=list(pays.values()) if isinstance(pays,dict) else pays
  result.append({'id_interno':local,'erp_id':erp_id,'name':name,'fantasy':str(row.get('fantasia') or '').strip(),'cpf_cnpj':str(row.get('cpf_cnpj') or ''),'period':period,'status':STATUSES[status],'situacao_cadastro_erp':status,'ativo_erp':status!='2','periodicidade_pendente':status!='2' and not period_ok,'type':old.get('type','NORMAL') if old else 'NORMAL','price_index':table,'tabela_preco_erp_id':str(row.get('tabela') or '0'),'condicoes_pagamento_erp':[{'erp_id':str(p.get('id') or ''),'descricao':str(p.get('desc') or ''),'tipo':str(p.get('tipo') or '')} for p in pays if isinstance(p,dict)],'correspondencia_anterior':method})
 # Sem retorno no ERP não pode ser selecionado, mas mantém identidade para histórico.
 for old in [*previous,*baseline]:
  if old['id_interno'] not in used:
   result.append({**old,'ativo_erp':False,'historical_only':True});used.add(old['id_interno'])
 return result,{'total_erp':len(rows),'ativos':sum(c.get('ativo_erp',True) for c in result),'inativos':sum(r.get('sit_cadastro')=='2' for r in rows),'vinculos_preservados':linked,'periodicidades_pendentes':sum(c.get('periodicidade_pendente',False) for c in result if c.get('ativo_erp',True))}

def apply(con,rows,baseline):
 init(con);previous=cached(con) or []
 clients,counts=map_clients(rows,baseline,previous)
 # Usa identificador legado reservado apenas para histórico, nunca como ID de envio ao ERP.
 con.execute('DELETE FROM erp_clientes')
 for c in clients:con.execute('INSERT INTO erp_clientes VALUES(?,?,?)',(str(c.get('erp_id') or 'HIST:'+c['id_interno']),c['id_interno'],json.dumps(c,ensure_ascii=False)))
 state={**counts,'ultima_atualizacao':datetime.now(timezone.utc).isoformat(),'erro':None,'source':'ERPFlex /api/clientes'}
 con.execute('INSERT OR REPLACE INTO erp_clientes_estado VALUES(1,?)',(json.dumps(state,ensure_ascii=False),));return state


def local_time(value=None):
    try:
        from zoneinfo import ZoneInfo
        tz=ZoneInfo('America/Sao_Paulo')
    except Exception: tz=timezone(timedelta(hours=-3))
    return (value or datetime.now(timezone.utc)).astimezone(tz)

def daily_due(state,now=None):
    current=local_time(now);due=current.replace(hour=3,minute=0,second=0,microsecond=0)
    if current<due: due-=timedelta(days=1)
    previous=(state or {}).get('ultima_atualizacao')
    if not previous:return True
    return datetime.fromisoformat(previous).astimezone(due.tzinfo)<due

class Sync:
 def __init__(self,db_factory,baseline,config):
  self.db=db_factory;self.baseline=baseline;self.config=config;self.running=False;self.error=None;self.stop_event=threading.Event();self.started=False
 def status(self):
  with self.db() as con:state=snapshot(con) or {}
  return {**state,'configurado':has_access(),'em_execucao':self.running,'ultimo_erro':self.error or state.get('erro'),'automatico':has_access(),'frequencia':'Diariamente às 03:00 (horário de Brasília)','limite_tentativas_diarias':1}
 def synchronize(self,credentials=None):
  if not LOCK.acquire(blocking=False):raise ValueError('Uma atualização de clientes já está em execução.')
  owner=uuid.uuid4().hex;leased=False;self.running=True
  try:
   with self.db() as con:
    con.execute('BEGIN IMMEDIATE');init(con)
    old=con.execute('SELECT owner,expires FROM erp_clientes_lease WHERE id=1').fetchone()
    if old and old[1]>time.time():raise ValueError('Uma atualização de clientes já está em execução.')
    con.execute('INSERT OR REPLACE INTO erp_clientes_lease VALUES(1,?,?)',(owner,time.time()+1800));leased=True
   access=credentials or load_access();rows,_=fetch_clients(access['username'],access['password'],lambda message:None)
   baseline=json.loads(self.baseline.read_text(encoding='utf-8-sig'))['clients']
   with self.db() as con:
    con.execute('BEGIN IMMEDIATE');state=apply(con,rows,baseline)
   self.error=None;return state
  except Exception:
   self.error='Não foi possível atualizar Clientes. A última base válida foi mantida. Confira o acesso ao ERP e a rede.'
   if leased:
    with self.db() as con:
     state=snapshot(con) or {};state['erro']=self.error
     con.execute('INSERT OR REPLACE INTO erp_clientes_estado VALUES(1,?)',(json.dumps(state,ensure_ascii=False),))
   raise
  finally:
   if leased:
    with self.db() as con:con.execute('DELETE FROM erp_clientes_lease WHERE id=1 AND owner=?',(owner,))
   self.running=False;LOCK.release()
 def run_daily(self):
  if not has_access():return False
  with self.db() as con:
   con.execute('BEGIN IMMEDIATE');init(con)
   if not daily_due(snapshot(con)):return False
   day=local_time().date().isoformat();row=con.execute('SELECT quantidade FROM erp_clientes_tentativas WHERE dia=?',(day,)).fetchone()
   if row and row[0]>=1:return False
   con.execute('INSERT INTO erp_clientes_tentativas VALUES(?,1) ON CONFLICT(dia) DO UPDATE SET quantidade=quantidade+1',(day,))
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
