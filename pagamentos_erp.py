"""Formas de pagamento ERP: IDs estáveis e regras locais preservadas."""
import json,threading,time,uuid
from datetime import datetime,timezone,timedelta
from clientes_erp import ACCESS,has_access,load_access,local_time
from consultar_pagamentos_erpflex import fetch_payments
from parametros import normalize
LOCK=threading.Lock()

def init(con):
 con.execute('CREATE TABLE IF NOT EXISTS erp_pagamentos(erp_id TEXT PRIMARY KEY,data_json TEXT NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_pagamentos_estado(id INTEGER PRIMARY KEY CHECK(id=1),data_json TEXT NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_pagamentos_lease(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT NOT NULL,expires REAL NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS erp_pagamentos_tentativas(dia TEXT PRIMARY KEY,quantidade INTEGER NOT NULL)')
def snapshot(con):
 if not con.execute("SELECT 1 FROM sqlite_master WHERE name='erp_pagamentos_estado'").fetchone():return None
 row=con.execute('SELECT data_json FROM erp_pagamentos_estado WHERE id=1').fetchone();return json.loads(row[0]) if row else None
def cached(con):
 if not (snapshot(con) or {}).get('ultima_atualizacao'):return None
 return [json.loads(r[0]) for r in con.execute('SELECT data_json FROM erp_pagamentos ORDER BY erp_id')]
def map_payments(rows,baseline,previous):
 if not rows:raise ValueError('Cadastro vazio; base anterior preservada.')
 ids=[str(r.get('formas_pags_id') or '').strip() for r in rows]
 if not all(ids) or len(set(ids))!=len(ids):raise ValueError('IDs ausentes ou duplicados.')
 aliases={'PIX':'PIX','CARTAODEBITO':'DEBITO','CARTAODEDEBITO':'DEBITO','CARTAOCREDITO1X':'CREDITO_1X','CARTAODECREDITO1X':'CREDITO_1X','CARTAOCREDITO2X':'CREDITO_2X','CARTAODECREDITO2X':'CREDITO_2X','DINHEIRO':'DINHEIRO','FATURADO30DDL':'FATURADO_30_DDL','FATURADODIA05':'FATURADO_DIA_05','FATURADODIA10':'FATURADO_DIA_10','FATURADODIA15':'FATURADO_DIA_15'}
 old_by_id={str(p['Id_CondPgtoERP']):p for p in previous if p.get('Id_CondPgtoERP')}
 base={p['key']:p for p in baseline};used=set();result=[]
 for r,sid in zip(rows,ids):
  name=str(r.get('desccricao') or '').strip()
  if not name:raise ValueError('Forma de pagamento sem descrição.')
  old=old_by_id.get(sid);key=old['key'] if old else aliases.get(normalize(name),('FATURADO_ERP_' if normalize(name).startswith('FATURADO') else 'ERP_PAG_')+sid)
  if key in used:raise ValueError('Mais de uma forma corresponde à mesma opção operacional. Base preservada.')
  used.add(key);billing=key.startswith('FATURADO_');operational=key in base or billing
  result.append({'key':key,'label':name,'Id_CondPgtoERP':sid,'erp_id':sid,'operacional':operational,'faturado':billing,'recebimento_imediato':key in base,'selecionavel':operational,'historical_only':False,'situacao_erp':None,**{k:r.get(k) for k in ('formas_pags_tipo','tipo_pagto','cartao_id','banco_id','parcelas','intervalo','dia_vencimento','data_alteracao')}})
 for old in previous:
  if str(old.get('Id_CondPgtoERP')) not in ids:result.append({**old,'selecionavel':False,'historical_only':True})
 return result

def resolve_invoice(client,catalog):
 """Sempre resolve pelo vínculo do cliente; periodicidade não determina condição."""
 methods={str(p['Id_CondPgtoERP']):p for p in catalog if p.get('selecionavel') and p.get('faturado')}
 ids={str(p.get('erp_id')) for p in client.get('condicoes_pagamento_erp',[]) if str(p.get('erp_id')) in methods}
 if ids:
  selected=min(ids,key=lambda value:(0,int(value)) if value.isdecimal() else (1,value))
  method=methods[selected]
  return {'status':'VINCULADA','erp_id':method['Id_CondPgtoERP'],'descricao':method['label'],'cliente_erp_id':client.get('erp_id')}
 return {'status':'SEM_VINCULO','erp_id':None,'descricao':None,'cliente_erp_id':client.get('erp_id'),'candidatos':sorted(ids)}

def invoice_registration_error(client,catalog):
 # Cadastros legados anteriores à API não têm este campo; snapshots continuam legíveis.
 if normalize(client.get('period')) not in ('MENSAL','QUINZENAL') or 'condicoes_pagamento_erp' not in client:return None
 if resolve_invoice(client,catalog)['status']=='VINCULADA':return None
 name=client.get('fantasy') or client.get('name') or client.get('id_interno')
 return f'Cliente {name} ({client.get("id_interno")}): periodicidade {client.get("period")} sem condição de pagamento Faturado vinculada no ERP. Solicite à administração o cadastro/vínculo da condição no ERP e a atualização de Clientes na aplicação. A OS não pode avançar até a correção.'

def bind_payload(payload,catalog):
 """Aplica IDs atuais no servidor; snapshots históricos não são remapeados."""
 methods=catalog['paymentConditions']
 current=next((c for c in catalog['clients'] if str(c['id_interno'])==str(payload.get('cliente_id'))),None)
 error=invoice_registration_error(current,methods) if current else None
 if error:raise ValueError(error)
 if payload.get('pagamento_os')=='FATURADO':
  client=next((c for c in catalog['clients'] if str(c['id_interno'])==str(payload.get('cliente_id'))),None)
  if client is None:raise ValueError('Cliente não encontrado para vincular condição faturada.')
  resolved=resolve_invoice(client,methods)
  payload['condicao_faturada_erp']=resolved
  payload['Id_CondPgtoERP']=resolved['erp_id']
  return
 available={p['key']:p for p in methods if p.get('recebimento_imediato') and p.get('selecionavel')}
 for payment in payload.get('pagamentos') or []:
  method=available.get(payment.get('condicao_pagamento'))
  if not method:raise ValueError('Forma de pagamento indisponível no cadastro atual.')
  real_id=str(method['Id_CondPgtoERP'])
  if payment.get('Id_CondPgtoERP') and str(payment['Id_CondPgtoERP'])!=real_id:raise ValueError('Forma de pagamento com vínculo ERP divergente. Recarregue a OS.')
  payment['Id_CondPgtoERP']=real_id

def invoice_for_export(client,catalog):
 """Chamar com cadastro ATUAL na futura saída ERP, nunca testar condições por tentativa."""
 resolved=resolve_invoice(client,catalog)
 if resolved['status']!='VINCULADA':raise ValueError('Condição faturada do cliente pendente de conferência; envio ao ERP não liberado.')
 return resolved

def apply(con,rows,baseline,requests=0):
 init(con);items=map_payments(rows,baseline,cached(con) or [])
 con.execute('DELETE FROM erp_pagamentos')
 for item in items:con.execute('INSERT INTO erp_pagamentos VALUES(?,?)',(item['Id_CondPgtoERP'],json.dumps(item,ensure_ascii=False)))
 state={'total_erp':len(rows),'formas_operacionais':sum(p['selecionavel'] for p in items),'formas_faturadas':sum(p.get('faturado',False) and p['selecionavel'] for p in items),'formas_recebimento_imediato':sum(p.get('recebimento_imediato',False) and p['selecionavel'] for p in items),'outras_formas':sum(not p['operacional'] and not p['historical_only'] for p in items),'situacao_informada_pelo_erp':False,'ultima_atualizacao':datetime.now(timezone.utc).isoformat(),'erro':None,'requisicoes_ultima_carga':requests,'source':'ERPFlex /v2/formas_pagamentos'}
 con.execute('INSERT OR REPLACE INTO erp_pagamentos_estado VALUES(1,?)',(json.dumps(state,ensure_ascii=False),));return state

def daily_due(state,now=None):
 current=local_time(now);due=current.replace(hour=3,minute=20,second=0,microsecond=0)
 if current<due:due-=timedelta(days=1)
 previous=(state or {}).get('ultima_atualizacao')
 return not previous or datetime.fromisoformat(previous).astimezone(due.tzinfo)<due

class Sync:
 def __init__(self,db_factory,baseline,config):
  self.db=db_factory;self.baseline=baseline;self.config=config;self.running=False;self.error=None;self.stop_event=threading.Event();self.started=False
 def status(self):
  with self.db() as con:state=snapshot(con) or {}
  return {**state,'configurado':has_access(),'em_execucao':self.running,'ultimo_erro':self.error or state.get('erro'),'automatico':has_access(),'frequencia':'Diariamente às 03:20 (horário de Brasília)','limite_tentativas_diarias':1}
 def synchronize(self,credentials=None,preloaded=None):
  if not LOCK.acquire(blocking=False):raise ValueError('Uma atualização de pagamentos já está em execução.')
  owner=uuid.uuid4().hex;leased=False;self.running=True
  try:
   with self.db() as con:
    con.execute('BEGIN IMMEDIATE');init(con)
    old=con.execute('SELECT owner,expires FROM erp_pagamentos_lease WHERE id=1').fetchone()
    if old and old[1]>time.time():raise ValueError('Uma atualização de pagamentos já está em execução.')
    con.execute('INSERT OR REPLACE INTO erp_pagamentos_lease VALUES(1,?,?)',(owner,time.time()+1800));leased=True
   if preloaded is None:
    access=credentials or load_access();rows,requests=fetch_payments(access['username'],access['password'],lambda message:None)
   else:rows,requests=preloaded
   baseline=json.loads(self.baseline.read_text(encoding='utf-8-sig'))['paymentConditions']
   with self.db() as con:
    con.execute('BEGIN IMMEDIATE');state=apply(con,rows,baseline,requests)
   self.error=None;return state
  except Exception:
   self.error='Não foi possível atualizar Formas de pagamento. A última base válida foi mantida. Confira o acesso ao ERP e a rede.'
   if leased:
    with self.db() as con:
     state=snapshot(con) or {};state['erro']=self.error
     con.execute('INSERT OR REPLACE INTO erp_pagamentos_estado VALUES(1,?)',(json.dumps(state,ensure_ascii=False),))
   raise
  finally:
   if leased:
    with self.db() as con:con.execute('DELETE FROM erp_pagamentos_lease WHERE id=1 AND owner=?',(owner,))
   self.running=False;LOCK.release()
 def run_daily(self):
  if not has_access():return False
  with self.db() as con:
   con.execute('BEGIN IMMEDIATE');init(con)
   if not daily_due(snapshot(con)):return False
   day=local_time().date().isoformat();row=con.execute('SELECT quantidade FROM erp_pagamentos_tentativas WHERE dia=?',(day,)).fetchone()
   if row and row[0]>=1:return False
   con.execute('INSERT INTO erp_pagamentos_tentativas VALUES(?,1) ON CONFLICT(dia) DO UPDATE SET quantidade=quantidade+1',(day,))
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
