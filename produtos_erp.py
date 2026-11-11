"""Catálogo completo de produtos e serviços; projeção operacional mantém os vínculos existentes."""
import json,threading,time,uuid,unicodedata
from collections import Counter
from datetime import datetime,timezone,timedelta
from clientes_erp import ACCESS,has_access,load_access,local_time
from consultar_produtos_erpflex import fetch_products
LOCK=threading.Lock()
def init(con):
 con.execute('CREATE TABLE IF NOT EXISTS erp_produtos(erp_id TEXT PRIMARY KEY,local_id TEXT NOT NULL UNIQUE,data_json TEXT NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_produtos_estado(id INTEGER PRIMARY KEY CHECK(id=1),data_json TEXT NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_produtos_lease(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT NOT NULL,expires REAL NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_produtos_tentativas(dia TEXT PRIMARY KEY,quantidade INTEGER NOT NULL)')
def snapshot(con):
 if not con.execute("SELECT 1 FROM sqlite_master WHERE name='erp_produtos_estado'").fetchone():return None
 row=con.execute('SELECT data_json FROM erp_produtos_estado WHERE id=1').fetchone();return json.loads(row[0]) if row else None
def cached(con):
 if not (snapshot(con) or {}).get('ultima_atualizacao'):return None
 return [json.loads(r[0]) for r in con.execute('SELECT data_json FROM erp_produtos ORDER BY local_id')]
def is_service(row):
 category=''.join(c for c in unicodedata.normalize('NFKD',str(row.get('categoria') or '')) if not unicodedata.combining(c))
 category=' '.join(category.casefold().split())
 return str(row.get('codigo') or '').strip().startswith('2') and category!='aporte de socios'

def map_products(rows,baseline,previous):
 if not rows:raise ValueError('O ERP retornou cadastro vazio. A última base foi preservada.')
 ids=[str(r.get('id') or '').strip() for r in rows];codes=Counter(str(r.get('codigo') or '').strip() for r in rows)
 if any(not sid for sid in ids) or len(set(ids))!=len(ids):raise ValueError('IDs ausentes ou duplicados no cadastro.')
 old_by_id={str(r['erp_id']):r for r in previous if r.get('erp_id')};base_by_code={r['code']:r for r in baseline};used=set();result=[];linked=0
 for r in rows:
  sid=str(r['id']);code=str(r.get('codigo') or '').strip();name=str(r.get('descricao') or '').strip();flag=str(r.get('ativo_inativo'))
  if not name or flag not in ('0','1'):raise ValueError('Item sem descrição ou situação desconhecida. Catálogo não atualizado.')
  old=old_by_id.get(sid)
  if old is None and code and codes[code]==1:old=base_by_code.get(code)
  local=old['id_interno'] if old else 'ERPPROD_'+sid
  if local in used:raise ValueError('Conflito de identidade de produtos/serviços.')
  used.add(local);service=is_service(r);operational=service and bool(old and old.get('operacional',old.get('kind') in ('Individual','KIT')))
  # Mudança de código exige revalidar o vínculo de preços, sem remapear valores.
  price_code=old.get('codigo_preco_legado',old.get('code')) if old else None
  selected=operational and flag=='1' and price_code==code
  linked+=bool(old and old.get('kind') in ('Individual','KIT'))
  result.append({'id_interno':local,'erp_id':sid,'code':code,'name':name,'kind':old.get('kind') if old else None,'servico_erp':service,'operacional':operational,'ativo_erp':flag=='1','selecionavel':selected,'codigo_preco_legado':price_code,'vinculo_preco_pendente':operational and price_code!=code,'produto_servico':r.get('produto_servico'),'tipo_fiscal':r.get('tipo'),'categoria':r.get('categoria'),'categoria_erp_id':r.get('IDCategoria'),'subcategoria':r.get('subcategoria'),'subcategoria_erp_id':r.get('IDSubcategoria'),'unidade':r.get('unidade'),'unidade_erp_id':r.get('unidade_id'),'data_alteracao_erp':r.get('data_alteracao') or r.get('dt_alteracao'),'historical_only':False})
 for old in [*previous,*baseline]:
  if old['id_interno'] not in used:result.append({**old,'ativo_erp':False,'selecionavel':False,'historical_only':True});used.add(old['id_interno'])
 return result,{'total_erp':len(rows),'servicos_erp':sum(is_service(r) for r in rows),'servicos_ativos_erp':sum(is_service(r) and str(r.get('ativo_inativo'))=='1' for r in rows),'ativos':sum(str(r.get('ativo_inativo'))=='1' for r in rows),'inativos':sum(str(r.get('ativo_inativo'))=='0' for r in rows),'servicos_operacionais':sum(r.get('selecionavel',False) for r in result),'vinculos_preservados':linked,'outros_itens':sum(not r.get('operacional',False) and not r.get('historical_only') for r in result),'precos_alterados':False}
def apply(con,rows,baseline,requests=0,import_prices=False):
 init(con);previous=cached(con) or [];items,counts=map_products(rows,baseline,previous)
 import precos_erp
 price_state=None
 if import_prices or precos_erp.snapshot(con):
  price_state=precos_erp.apply(con,rows,requests)
  counts['precos_alterados']=True
  counts['valores_precos_importados']=price_state['valores']
 con.execute('DELETE FROM erp_produtos')
 for item in items:con.execute('INSERT INTO erp_produtos VALUES(?,?,?)',(str(item.get('erp_id') or 'HIST:'+item['id_interno']),item['id_interno'],json.dumps(item,ensure_ascii=False)))
 state={**counts,'ultima_atualizacao':datetime.now(timezone.utc).isoformat(),'erro':None,'requisicoes_ultima_carga':requests,'source':'ERPFlex /api/produtos/'}
 con.execute('INSERT OR REPLACE INTO erp_produtos_estado VALUES(1,?)',(json.dumps(state,ensure_ascii=False),));return state
def daily_due(state,now=None):
 current=local_time(now);due=current.replace(hour=3,minute=10,second=0,microsecond=0)
 if current<due:due-=timedelta(days=1)
 previous=(state or {}).get('ultima_atualizacao')
 return not previous or datetime.fromisoformat(previous).astimezone(due.tzinfo)<due

class Sync:
 def __init__(self,db_factory,baseline,config):
  self.db=db_factory;self.baseline=baseline;self.config=config;self.running=False;self.error=None;self.stop_event=threading.Event();self.started=False
 def status(self):
  with self.db() as con:state=snapshot(con) or {}
  return {**state,'configurado':has_access(),'em_execucao':self.running,'ultimo_erro':self.error or state.get('erro'),'automatico':has_access(),'frequencia':'Diariamente às 03:10 (horário de Brasília)','limite_tentativas_diarias':1}
 def synchronize(self,credentials=None,preloaded=None):
  if not LOCK.acquire(blocking=False):raise ValueError('Uma atualização de produtos já está em execução.')
  owner=uuid.uuid4().hex;leased=False;self.running=True
  try:
   with self.db() as con:
    con.execute('BEGIN IMMEDIATE');init(con)
    old=con.execute('SELECT owner,expires FROM erp_produtos_lease WHERE id=1').fetchone()
    if old and old[1]>time.time():raise ValueError('Uma atualização de produtos já está em execução.')
    con.execute('INSERT OR REPLACE INTO erp_produtos_lease VALUES(1,?,?)',(owner,time.time()+1800));leased=True
   if preloaded is None:
    access=credentials or load_access();rows,requests=fetch_products(access['username'],access['password'],lambda message:None)
   else:rows,requests=preloaded
   baseline=json.loads(self.baseline.read_text(encoding='utf-8-sig'))['services']
   with self.db() as con:
    con.execute('BEGIN IMMEDIATE');state=apply(con,rows,baseline,requests)
   self.error=None;return state
  except Exception:
   self.error='Não foi possível atualizar Produtos e serviços. A última base válida foi mantida. Confira o acesso ao ERP e a rede.'
   if leased:
    with self.db() as con:
     state=snapshot(con) or {};state['erro']=self.error
     con.execute('INSERT OR REPLACE INTO erp_produtos_estado VALUES(1,?)',(json.dumps(state,ensure_ascii=False),))
   raise
  finally:
   if leased:
    with self.db() as con:con.execute('DELETE FROM erp_produtos_lease WHERE id=1 AND owner=?',(owner,))
   self.running=False;LOCK.release()
 def run_daily(self):
  if not has_access():return False
  with self.db() as con:
   con.execute('BEGIN IMMEDIATE');init(con)
   if not daily_due(snapshot(con)):return False
   day=local_time().date().isoformat();row=con.execute('SELECT quantidade FROM erp_produtos_tentativas WHERE dia=?',(day,)).fetchone()
   if row and row[0]>=1:return False
   con.execute('INSERT INTO erp_produtos_tentativas VALUES(?,1) ON CONFLICT(dia) DO UPDATE SET quantidade=quantidade+1',(day,))
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
