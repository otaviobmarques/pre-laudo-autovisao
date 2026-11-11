import parametros as policy
import clientes_erp
import produtos_erp
import pagamentos_erp
import transportadoras_erp
import precos_erp
import empresas_erp
import socket
import os, json, base64, mimetypes, re, sqlite3, uuid, hashlib, secrets, hmac
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
# Hospedagem: DATA_DIR aponta para o volume persistente. Sem ela, tudo fica na pasta do programa.
DATA_DIR = Path(os.environ.get("DATA_DIR") or ROOT).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "pre_laudo_teste.db"
COMPROVANTES_DIR = ROOT / "comprovantes"
def _persistent(name, is_dir):
    """Mantém em DATA_DIR o que o programa grava ao lado do código."""
    local=ROOT/name
    if DATA_DIR==ROOT:
        if is_dir: local.mkdir(exist_ok=True)
        return local
    target=DATA_DIR/name
    if is_dir:
        target.mkdir(parents=True,exist_ok=True)
        # Um atalho que aponte para outro lugar (ex.: sobra de outro ambiente) é refeito para o volume.
        if local.is_symlink() and local.resolve()!=target.resolve(): local.unlink()
        if not local.is_symlink() and not local.exists(): local.symlink_to(target,target_is_directory=True)
        return local
    if not target.exists() and local.exists(): target.write_bytes(local.read_bytes())
    return target
_persistent("comprovantes",True)
_persistent("work",True)

def load_env():
    p = ROOT / "config.env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k,v=line.split("=",1)
        os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
load_env()

PROMPT = """Você é um extrator de dados de comprovantes de pagamento automotivos.
Leia SOMENTE o que estiver visível na imagem. Não corrija datas com base no dia atual e não invente caracteres ilegíveis.

Retorne exclusivamente JSON válido, sem markdown, neste formato:
{
  "condicao": "CARTAO_DEBITO" | "CARTAO_CREDITO" | "PIX" | null,
  "valor": number | null,
  "data_hora": "YYYY-MM-DDTHH:MM:SS" | null,
  "identificador_transacao": string | null,
  "tipo_identificador": "AUT" | "PIX_ID_TRANSACAO" | null,
  "observacao": string | null
}

Regras:
- Cartão: identificador_transacao é especificamente o conteúdo associado ao campo AUT. Pode ser alfanumérico; preserve exatamente os caracteres.
- PIX: identificador_transacao é especificamente o conteúdo associado a ID/TRANSAÇÃO.
- Não use DOC, CV, AID, ARQC ou número do cartão como identificador.
- Preserve zeros à esquerda.
- Se um campo estiver realmente ilegível, retorne null e explique brevemente em observacao.
"""

class ManagedConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()

def db():
    con=sqlite3.connect(DB_PATH, timeout=30, factory=ManagedConnection)
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory=sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def ensure_column(con, table, col, decl):
    cols={r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

def init_db():
    con=db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS os (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      id_interno TEXT UNIQUE NOT NULL,
      erp_id TEXT,
      aberta_em TEXT, enviada_em TEXT,
      unidade_id TEXT, unidade_nome TEXT,
      vistoriador TEXT,
      cliente_id TEXT, cliente_erp_id TEXT, cliente_nome TEXT,
      placa TEXT, marca TEXT, modelo TEXT,
      local_vistoria TEXT,
      condutor TEXT, cpf TEXT, email TEXT, celular TEXT,
      requisicao_cliente TEXT,
      pagamento_os TEXT, status_pagamento TEXT,
      periodicidade_cliente TEXT,
      tabela_preco_indice TEXT, tabela_preco_erp_id TEXT,
      condicao_pagamento TEXT, Id_CondPgtoERP TEXT,
      total_centavos INTEGER,
      status_conferencia TEXT,
      payload_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS itens_os (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      os_id INTEGER NOT NULL REFERENCES os(id) ON DELETE CASCADE,
      servico TEXT,
      servico_erp_id TEXT,
      preco_referencia_centavos INTEGER,
      preco_cobrado_centavos INTEGER,
      preco_origem TEXT,
      preco_editado INTEGER DEFAULT 0,
      item_json TEXT
    );
    CREATE TABLE IF NOT EXISTS pagamentos (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      os_id INTEGER NOT NULL REFERENCES os(id) ON DELETE CASCADE,
      ordem INTEGER,
      condicao_pagamento TEXT,
      Id_CondPgtoERP TEXT,
      valor_centavos INTEGER,
      validated INTEGER DEFAULT 0,
      cashCode TEXT,
      comprovante_nome TEXT,
      comprovante_anexado INTEGER DEFAULT 0,
      comprovante_arquivo TEXT,
      vision_condicao TEXT,
      vision_valor_centavos INTEGER,
      vision_data_hora TEXT,
      identificador_transacao TEXT,
      tipo_identificador TEXT,
      check_forma INTEGER,
      check_valor INTEGER,
      check_tempo INTEGER,
      needs_review INTEGER,
      pagamento_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_pag_idtrans ON pagamentos(identificador_transacao);
    """)
    # Migrate older DBs safely.
    os_cols = {
      "unidade_id":"TEXT","unidade_nome":"TEXT","vistoriador":"TEXT",
      "cliente_id":"TEXT","cliente_erp_id":"TEXT","cliente_nome":"TEXT",
      "placa":"TEXT","marca":"TEXT","modelo":"TEXT","local_vistoria":"TEXT",
      "condutor":"TEXT","cpf":"TEXT","email":"TEXT","celular":"TEXT",
      "requisicao_cliente":"TEXT","pagamento_os":"TEXT","status_pagamento":"TEXT",
      "periodicidade_cliente":"TEXT","tabela_preco_indice":"TEXT",
      "tabela_preco_erp_id":"TEXT","condicao_pagamento":"TEXT",
      "Id_CondPgtoERP":"TEXT","total_centavos":"INTEGER","status_conferencia":"TEXT",
      "payload_json":"TEXT"
    }
    for c,d in os_cols.items(): ensure_column(con,"os",c,d)
    pay_cols = {
      "ordem":"INTEGER","condicao_pagamento":"TEXT","Id_CondPgtoERP":"TEXT",
      "valor_centavos":"INTEGER","validated":"INTEGER","cashCode":"TEXT",
      "comprovante_nome":"TEXT","comprovante_anexado":"INTEGER",
      "comprovante_arquivo":"TEXT","vision_condicao":"TEXT",
      "vision_valor_centavos":"INTEGER","vision_data_hora":"TEXT",
      "identificador_transacao":"TEXT","tipo_identificador":"TEXT",
      "check_forma":"INTEGER","check_valor":"INTEGER","check_tempo":"INTEGER",
      "needs_review":"INTEGER","pagamento_json":"TEXT"
    }
    for c,d in pay_cols.items(): ensure_column(con,"pagamentos",c,d)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS usuarios_vistoriadores (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      login TEXT NOT NULL UNIQUE COLLATE NOCASE,
      vistoriador_id TEXT NOT NULL UNIQUE,
      vistoriador_nome TEXT NOT NULL,
      unidade_id TEXT NOT NULL,
      unidade_nome TEXT NOT NULL,
      senha_hash TEXT NOT NULL,
      ativo INTEGER NOT NULL DEFAULT 1 CHECK(ativo IN (0,1)),
      criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)
    con.execute("""CREATE TABLE IF NOT EXISTS auditoria_os (
      id INTEGER PRIMARY KEY AUTOINCREMENT, os_id INTEGER NOT NULL,
      registrado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      operador TEXT NOT NULL, justificativa TEXT NOT NULL,
      justificativa_financeira TEXT, diferenca_centavos INTEGER NOT NULL,
      alteracoes_json TEXT NOT NULL,
      anterior_json TEXT NOT NULL, novo_json TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS liberacoes_administrativas (
      id INTEGER PRIMARY KEY AUTOINCREMENT, os_id INTEGER NOT NULL,
      operador TEXT NOT NULL, liberado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      status_anterior TEXT, divergencias_json TEXT NOT NULL,
      justificativas_json TEXT NOT NULL, total_centavos INTEGER NOT NULL,
      comprovado_centavos INTEGER NOT NULL, diferenca_centavos INTEGER NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS usuarios_administrativos (
      id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL,
      login TEXT UNIQUE NOT NULL COLLATE NOCASE, senha_hash TEXT NOT NULL,
      ativo INTEGER NOT NULL DEFAULT 1, criado_em TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS integracoes_chaves (
      nome TEXT PRIMARY KEY, chave TEXT NOT NULL, modelo TEXT, provedor TEXT,
      operador TEXT NOT NULL, atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    con.commit(); con.close()

def hash_password(password):
    salt=secrets.token_bytes(16)
    rounds=310000
    digest=hashlib.pbkdf2_hmac("sha256",password.encode("utf-8"),salt,rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"

# Tokens temporários apenas na memória: reiniciar o servidor encerra sessões.
SESSOES = {}
def verify_password(password, encoded):
    try:
        scheme,rounds,salt,digest=encoded.split("$")
        if scheme!="pbkdf2_sha256": return False
        computed=hashlib.pbkdf2_hmac("sha256",password.encode("utf-8"),bytes.fromhex(salt),int(rounds))
        return hmac.compare_digest(computed,bytes.fromhex(digest))
    except (ValueError,TypeError): return False

def get_session(handler):
    header=handler.headers.get("Authorization","")
    if not header.startswith("Bearer "): return None
    token=header[7:].strip()
    sess=SESSOES.get(token)
    if not sess: return None
    if datetime.now(timezone.utc)>sess["expires"]:
        SESSOES.pop(token,None);return None
    with db() as con:
        user=con.execute("SELECT id,login,vistoriador_id,vistoriador_nome,unidade_id,unidade_nome,ativo FROM usuarios_vistoriadores WHERE id=?",(sess["id"],)).fetchone()
    if not user or not user["ativo"]: return None
    current=inspector_metadata(user)
    return current if current.get('ativo_erp',True) else None

def authenticate(login,password):
    with db() as con:
        user=con.execute("SELECT * FROM usuarios_vistoriadores WHERE login=? COLLATE NOCASE",(login.strip(),)).fetchone()
    if not user or not user["ativo"] or not verify_password(password,user["senha_hash"]): return None
    current=inspector_metadata(user)
    if not current.get('ativo_erp',True):return None
    token=secrets.token_urlsafe(40)
    SESSOES[token]={"id":user["id"],"expires":datetime.now(timezone.utc)+timedelta(hours=12)}
    return token,{k:v for k,v in current.items() if k in ("id","login","vistoriador_id","vistoriador_nome","unidade_id","unidade_nome","vistoriador_erp_id")}

CATALOGO_PATH = _persistent("catalogo_erp_base.json",False)
def catalogo():
    with CATALOGO_PATH.open(encoding="utf-8") as f:
        result=json.load(f)
    with db() as con:
        carriers=transportadoras_erp.cached(con)
        units=empresas_erp.unidades(con)
    if carriers is not None: result['vistoriadores']=carriers
    if units is not None: result['unidades']=units
    return result

def inspector_metadata(user):
    with db() as con: carriers=transportadoras_erp.cached(con)
    if carriers is None:return dict(user)
    record=next((r for r in carriers if r['id']==user['vistoriador_id']),None)
    return {**dict(user),'vistoriador_erp_id':record.get('erp_id') if record else None,'vistoriador_nome':record['nome'] if record else user['vistoriador_nome'],'ativo_erp':bool(record and record.get('ativo_erp'))}

def catalogo_mapas():
    c=catalogo()
    return ({str(x["id"]):x for x in c["vistoriadores"]},
            {str(x["id"]):x for x in c["unidades"]})

def list_users():
    with db() as con:
        users=[dict(r) for r in con.execute("""SELECT id,login,vistoriador_id,vistoriador_nome,
          unidade_id,unidade_nome,ativo,criado_em,atualizado_em
          FROM usuarios_vistoriadores ORDER BY login""")]
    return [inspector_metadata(user) for user in users]

def validate_user(body, editing=False):
    login=str(body.get("login") or "").strip().lower()
    vist=str(body.get("vistoriador_id") or "").strip()
    vnome=str(body.get("vistoriador_nome") or "").strip()
    unidade=str(body.get("unidade_id") or "").strip()
    unome=str(body.get("unidade_nome") or "").strip()
    pwd=body.get("senha") or ""
    if not re.fullmatch(r"[a-z0-9._-]{3,60}",login): raise ValueError("Login inválido (3 a 60 caracteres, letras, números, ponto, hífen ou _).")
    if not vist or not unidade: raise ValueError("Vistoriador e unidade são obrigatórios.")
    vistoriadores,unidades=catalogo_mapas()
    if vist not in vistoriadores: raise ValueError("Vistoriador não pertence à base ERP disponível.")
    if unidade not in unidades: raise ValueError("Unidade não pertence à base ERP disponível.")
    if body.get("ativo") and vistoriadores[vist].get("ativo_erp") is False: raise ValueError("Vistoriador inativo no ERP: não é possível habilitar seu acesso.")
    vnome=vistoriadores[vist]["nome"]
    unome=unidades[unidade]["nome"]
    if any(len(x)>120 for x in (vist,vnome,unidade,unome)): raise ValueError("Campo muito longo.")
    if pwd and (not isinstance(pwd,str) or len(pwd)<8 or len(pwd)>256): raise ValueError("Senha deve ter entre 8 e 256 caracteres.")
    if not editing and not pwd: raise ValueError("Senha obrigatória.")
    if body.get("ativo") not in (True,False): raise ValueError("Situação inválida.")
    return login,vist,vnome,unidade,unome,pwd,int(body["ativo"])

def save_data_url(data_url, os_internal, order, original_name=None):
    if not data_url or not isinstance(data_url,str) or not data_url.startswith("data:"):
        return None
    head,b64=data_url.split(",",1)
    mime=head.split(";")[0].replace("data:","").strip()
    ext=mimetypes.guess_extension(mime) or ".bin"
    if ext==".jpe": ext=".jpg"
    safe_os=re.sub(r"[^A-Za-z0-9_.-]+","_",str(os_internal))
    folder=COMPROVANTES_DIR/safe_os
    folder.mkdir(parents=True,exist_ok=True)
    filename=f"PAG-{order:02d}-{uuid.uuid4().hex[:8]}{ext}"
    path=folder/filename
    path.write_bytes(base64.b64decode(b64))
    return str(path.relative_to(ROOT)).replace("\\","/")

def save_document(data_url, os_internal, kind):
    if not isinstance(data_url,str) or not data_url.startswith("data:") or "," not in data_url:
        raise ValueError("Documento inválido")
    head,b64=data_url.split(",",1)
    mime=head.split(";",1)[0][5:].lower()
    ext={"image/jpeg":".jpg","image/png":".png","image/webp":".webp","application/pdf":".pdf"}.get(mime)
    if not ext: raise ValueError("Formato de documento não suportado")
    raw=base64.b64decode(b64,validate=True)
    if len(raw)>12*1024*1024: raise ValueError("Documento maior que 12 MB")
    folder=COMPROVANTES_DIR/re.sub(r"[^A-Za-z0-9_.-]+","_",str(os_internal))
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/("DOC-"+kind+"-"+uuid.uuid4().hex[:8]+ext)
    path.write_bytes(raw)
    return str(path.relative_to(ROOT)).replace("\\","/")

# Chaves de integração cadastradas pela Administração. A chave nunca volta inteira para a tela.
INTEGRACOES={
    "vision":{"titulo":"IA (Vision)","env_chave":"OPENAI_API_KEY","env_modelo":"OPENAI_MODEL","modelo_padrao":"gpt-5.5","provedor_padrao":"OpenAI"},
}
def integracao_config(nome):
    """Chave em uso: a cadastrada no painel tem prioridade sobre a variável de ambiente."""
    meta=INTEGRACOES[nome]
    with db() as con:
        row=con.execute("SELECT chave,modelo,provedor,operador,atualizado_em FROM integracoes_chaves WHERE nome=?",(nome,)).fetchone()
    if row and row["chave"]:
        return {"chave":row["chave"],"modelo":row["modelo"] or os.getenv(meta["env_modelo"]) or meta["modelo_padrao"],"provedor":row["provedor"] or meta["provedor_padrao"],"origem":"painel","operador":row["operador"],"atualizado_em":row["atualizado_em"]}
    env=os.getenv(meta["env_chave"]) or ""
    return {"chave":env,"modelo":(row["modelo"] if row else None) or os.getenv(meta["env_modelo"]) or meta["modelo_padrao"],"provedor":(row["provedor"] if row else None) or meta["provedor_padrao"],"origem":"ambiente" if env else None,"operador":None,"atualizado_em":None}
def integracao_status(nome):
    cfg=integracao_config(nome);chave=cfg.pop("chave")
    cfg.update({"integracao":nome,"configurada":bool(chave),"final_chave":chave[-4:] if len(chave)>=12 else ""})
    return cfg
def integracao_salvar(nome,body,operador):
    if nome not in INTEGRACOES: raise ValueError("Integração desconhecida.")
    chave=str(body.get("chave") or "").strip()
    modelo=str(body.get("modelo") or "").strip()[:80]
    provedor=str(body.get("provedor") or "").strip()[:40]
    if chave and (len(chave)<12 or len(chave)>400 or re.search(r"\s",chave)): raise ValueError("Chave inválida: confira se copiou o valor completo, sem espaços.")
    with db() as con:
        atual=con.execute("SELECT chave FROM integracoes_chaves WHERE nome=?",(nome,)).fetchone()
        if not chave:
            if not atual: raise ValueError("Informe a chave.")
            con.execute("UPDATE integracoes_chaves SET modelo=?,provedor=?,operador=?,atualizado_em=CURRENT_TIMESTAMP WHERE nome=?",(modelo,provedor,operador,nome))
        else:
            con.execute("""INSERT INTO integracoes_chaves(nome,chave,modelo,provedor,operador) VALUES(?,?,?,?,?)
              ON CONFLICT(nome) DO UPDATE SET chave=excluded.chave,modelo=excluded.modelo,provedor=excluded.provedor,operador=excluded.operador,atualizado_em=CURRENT_TIMESTAMP""",(nome,chave,modelo,provedor,operador))
def integracao_remover(nome):
    if nome not in INTEGRACOES: raise ValueError("Integração desconhecida.")
    with db() as con: con.execute("DELETE FROM integracoes_chaves WHERE nome=?",(nome,))
# Acesso à API do ERPFlex cadastrado pela Administração (usuário e senha). A senha nunca volta para a tela.
def erp_acesso_painel():
    try:
        with db() as con:
            row=con.execute("SELECT provedor,chave FROM integracoes_chaves WHERE nome='erpflex'").fetchone()
    except sqlite3.Error: return None
    return {"username":row["provedor"],"password":row["chave"]} if row and row["provedor"] and row["chave"] else None
clientes_erp.PANEL_ACCESS=erp_acesso_painel
def erp_acesso_status():
    origem=clientes_erp.access_source();usuario="";operador=None;atualizado=None
    if origem=="painel":
        with db() as con:
            row=con.execute("SELECT provedor,operador,atualizado_em FROM integracoes_chaves WHERE nome='erpflex'").fetchone()
        usuario,operador,atualizado=row["provedor"],row["operador"],row["atualizado_em"]
    elif origem=="ambiente": usuario=(clientes_erp.env_access() or {}).get("username","")
    return {"configurado":bool(origem),"origem":origem,"usuario":usuario,"operador":operador,"atualizado_em":atualizado}
def erp_acesso_salvar(body,operador):
    usuario=str(body.get("usuario") or "").strip();senha=str(body.get("senha") or "")
    if not usuario or len(usuario)>200: raise ValueError("Informe o usuário da API do ERP.")
    with db() as con:
        atual=con.execute("SELECT 1 FROM integracoes_chaves WHERE nome='erpflex'").fetchone()
        if not senha:
            if not atual: raise ValueError("Informe a senha da API do ERP.")
            con.execute("UPDATE integracoes_chaves SET provedor=?,operador=?,atualizado_em=CURRENT_TIMESTAMP WHERE nome='erpflex'",(usuario,operador))
        else:
            if len(senha)>400: raise ValueError("Senha inválida.")
            con.execute("""INSERT INTO integracoes_chaves(nome,chave,modelo,provedor,operador) VALUES('erpflex',?,'',?,?)
              ON CONFLICT(nome) DO UPDATE SET chave=excluded.chave,provedor=excluded.provedor,operador=excluded.operador,atualizado_em=CURRENT_TIMESTAMP""",(senha,usuario,operador))
def erp_acesso_remover():
    with db() as con: con.execute("DELETE FROM integracoes_chaves WHERE nome='erpflex'")

def integracao_testar_vision():
    cfg=integracao_config("vision")
    if not cfg["chave"]: raise ValueError("Nenhuma chave configurada.")
    from openai import OpenAI
    OpenAI(api_key=cfg["chave"],timeout=30).models.retrieve(cfg["modelo"])
    return cfg["modelo"]

def json_response(h, status, obj):
    raw=json.dumps(obj,ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type","application/json; charset=utf-8")
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    h.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    h.send_header("Content-Length",str(len(raw)))
    h.end_headers(); h.wfile.write(raw)


# Administração v9.3: permissões separadas, catálogo e auditoria.
EDITABLE_FIELDS = ("cliente_id","cliente_nome","placa","marca","modelo","local_vistoria",
 "condutor","cpf","email","celular","requisicao_cliente","pagamento_os")
ADMIN_SESSIONS={}
ADMIN_CATALOG=_persistent("catalogo_precos_v93.json",False)
def admin_catalog():
    result=json.loads(ADMIN_CATALOG.read_text(encoding="utf-8"))
    with db() as con:
        imported=clientes_erp.cached(con)
        products=produtos_erp.cached(con)
        payments=pagamentos_erp.cached(con)
        prices=precos_erp.cached(con)
    if products is not None: result['services']=[p for p in products if p.get('operacional',p.get('kind') in ('Individual','KIT'))]
    if imported is not None: result['clients']=imported
    if payments is not None: result['paymentConditions']=[p for p in payments if p.get('selecionavel')]
    if prices is not None: result['prices']=prices
    return result

CARRIER_SYNC=None
def carrier_sync():
    global CARRIER_SYNC
    if CARRIER_SYNC is None: CARRIER_SYNC=transportadoras_erp.Sync(db,CATALOGO_PATH,config_atual)
    return CARRIER_SYNC

PAYMENT_SYNC=None
def payment_sync():
    global PAYMENT_SYNC
    if PAYMENT_SYNC is None: PAYMENT_SYNC=pagamentos_erp.Sync(db,ADMIN_CATALOG,config_atual)
    return PAYMENT_SYNC

PRODUCT_SYNC=None
CLIENT_SYNC=None
def client_sync():
    global CLIENT_SYNC
    if CLIENT_SYNC is None: CLIENT_SYNC=clientes_erp.Sync(db,ADMIN_CATALOG,config_atual)
    return CLIENT_SYNC
def product_sync():
    global PRODUCT_SYNC
    if PRODUCT_SYNC is None: PRODUCT_SYNC=produtos_erp.Sync(db,ADMIN_CATALOG,config_atual)
    return PRODUCT_SYNC

def admin_user(handler):
    auth=handler.headers.get("Authorization","")
    token=auth[7:].strip() if auth.startswith("Bearer ") else ""
    sess=ADMIN_SESSIONS.get(token)
    if not sess or sess["expires"]<datetime.now(timezone.utc):
        ADMIN_SESSIONS.pop(token,None); return None
    with db() as con:
        u=con.execute("SELECT id,nome,login,ativo FROM usuarios_administrativos WHERE id=?",(sess["id"],)).fetchone()
    return dict(u) if u and u["ativo"] else None

def validate_catalog_item(it, client, cat):
    return policy.reference(it,client,cat)

def review_payment(p, original, require_vision=True):
    vision=p.get("comprovante_leitura") or {}
    value=vision.get("valor_centavos")
    mismatch=value is not None and int(value)!=int(p["valor_centavos"])
    validation=vision.get("validacao") or {}
    mismatch=mismatch or validation.get("methodOk") is False or validation.get("timeOk") is False
    if mismatch and len(str(p.get("justificativa_divergencia") or "").strip())<10:
        raise ValueError("Comprovante divergente: informe justificativa individual para o pagamento.")
    return mismatch

def classificar_os(payload, modo):
    """Decisão única para criação e edição: pendência real exige análise."""
    itens=payload.get("itens") or []
    pagamentos=payload.get("pagamentos") or []
    if not itens or modo not in ("A_VISTA", "FATURADO"):
        return "PENDENTE", "AGUARDANDO_JUSTIFICATIVAS"
    # Uma OS faturada regular não requer comprovante nem liberação humana.
    if modo=="FATURADO":
        return "LIBERADA_AUTOMATICAMENTE", "CONCLUIDA"
    if not pagamentos:
        return "PENDENTE", "AGUARDANDO_JUSTIFICATIVAS"
    total=sum(int(it.get("preco_cobrado_centavos") or 0) for it in itens)
    comprovado=0
    pendencias=[]
    justificadas=True
    for p in pagamentos:
        if p.get("condicao_pagamento")=="DINHEIRO":
            comprovado+=int(p.get("valor_centavos") or 0)
            continue
        if not p.get("comprovante_arquivo"):
            pendencias.append("Comprovante ausente")
            justificadas=False
            continue
        leitura=p.get("comprovante_leitura") or {}
        valor=leitura.get("valor_centavos")
        validacao=leitura.get("validacao") or {}
        if valor is None:
            pendencias.append("Valor do comprovante não identificado")
        else:
            comprovado+=int(valor)
        if (valor is not None and int(valor)!=int(p.get("valor_centavos") or 0)) or any(validacao.get(k) is False for k in ("methodOk","timeOk","valueOk")) or validacao.get("needsReview"):
            pendencias.append("Divergência no comprovante")
            if len(str(p.get("justificativa_divergencia") or "").strip())<10:
                justificadas=False
    if comprovado!=total:
        pendencias.append("Conciliação financeira divergente")
    if not pendencias:
        return "LIBERADA_AUTOMATICAMENTE", "CONCLUIDA"
    return "PENDENTE", ("AGUARDANDO_LIBERACAO" if justificadas else "AGUARDANDO_JUSTIFICATIVAS")

def reconciliar_status_os(con, osdata):
    """Reclassifica OS legadas somente quando a validação comprova liberação automática.

    Nunca altera uma liberação administrativa, status de ERP ou OS vinculada ao ERP.
    """
    atual=str(osdata.get("status_conferencia") or "").strip().upper()
    protegidos={"LIBERADA_ADMINISTRACAO", "SINCRONIZADA_ERP", "FALHA_SINCRONIZACAO"}
    if atual in protegidos or osdata.get("erp_id"):
        return False
    payload=json.loads(osdata.get("payload_json") or "{}")
    if payload.get("parametros_snapshot"): return False
    modo=str(osdata.get("pagamento_os") or payload.get("pagamento_os") or "").upper()
    calculado,_=classificar_os(payload,modo)
    # A reconciliação não deve inventar aprovação quando o cadastro está incompleto.
    if calculado!="LIBERADA_AUTOMATICAMENTE":
        return False
    if atual==calculado:
        return False
    cur=con.execute("UPDATE os SET status_conferencia=? WHERE id=? AND COALESCE(status_conferencia,'')=?",
                (calculado,osdata["id"],osdata.get("status_conferencia") or ""))
    if cur.rowcount==1:
        osdata["status_conferencia"]=calculado
        return True
    return False

def etapa_analise_administrativa(con, osdata):
    status=str(osdata.get("status_conferencia") or "").upper()
    if status in ("LIBERADA_ADMINISTRACAO", "LIBERADA_AUTOMATICAMENTE", "SINCRONIZADA_ERP", "FALHA_SINCRONIZACAO", "APROVADA", "APROVADO", "LIBERADA", "LIBERADO"):
        return "CONCLUIDA"
    try:
        payload=json.loads(osdata.get("payload_json") or "{}")
        _,etapa=classificar_os(payload,str(osdata.get("pagamento_os") or "").upper())
        if etapa=="CONCLUIDA": return "CONCLUIDA"
        if etapa=="AGUARDANDO_LIBERACAO":
            config=payload.get('parametros_snapshot')
            if config and 'justifications' in config:
                return 'AGUARDANDO_JUSTIFICATIVAS' if policy.justification_errors(payload,config,payload.get('_other_changes',False)) else 'AGUARDANDO_LIBERACAO'
            # Divergência de conciliação exige justificativa financeira persistida.
            itens=payload.get("itens") or []
            pays=payload.get("pagamentos") or []
            if str(osdata.get("pagamento_os") or "").upper()=="A_VISTA":
                total=sum(int(i.get("preco_cobrado_centavos") or 0) for i in itens)
                comprovado=sum(int(p.get("valor_centavos") or 0) if p.get("condicao_pagamento")=="DINHEIRO" else int((p.get("comprovante_leitura") or {}).get("valor_centavos") or 0) for p in pays)
                sem_valor=any(p.get("condicao_pagamento")!="DINHEIRO" and (p.get("comprovante_leitura") or {}).get("valor_centavos") is None for p in pays)
                if comprovado!=total or sem_valor:
                    ultima=con.execute("SELECT justificativa_financeira FROM auditoria_os WHERE os_id=? ORDER BY id DESC LIMIT 1",(osdata["id"],)).fetchone()
                    if not ultima or len(str(ultima["justificativa_financeira"] or "").strip())<10:
                        return "AGUARDANDO_JUSTIFICATIVAS"
        return etapa
    except (TypeError,ValueError,KeyError,AttributeError,sqlite3.Error):
        return "AGUARDANDO_JUSTIFICATIVAS"

def liberar_os_administrativamente(body):
    operador=str(body.get("_operador_autenticado") or "").strip()
    if not operador: raise PermissionError("Sessão administrativa obrigatória.")
    os_id=int(body.get("os_id"))
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row=con.execute("SELECT * FROM os WHERE id=?",(os_id,)).fetchone()
        if not row: raise ValueError("OS não encontrada.")
        osdata=dict(row)
        if osdata.get("erp_id"): raise ValueError("OS já vinculada ao ERP.")
        if str(osdata.get("status_conferencia") or "").upper()=="LIBERADA_AUTOMATICAMENTE":
            raise ValueError("OS já liberada automaticamente; não exige liberação administrativa.")
        if osdata.get("status_conferencia")=="LIBERADA_ADMINISTRACAO":
            raise ValueError("OS já liberada administrativamente.")
        if osdata.get("status_conferencia") in ("SINCRONIZADA_ERP","FALHA_SINCRONIZACAO"):
            raise ValueError("Status de integração não permite nova liberação.")
        payload=json.loads(osdata["payload_json"] or "{}")
        for index,text in enumerate(body.get('declaracoes_dinheiro') or []):
            if index<len(payload.get('pagamentos') or []) and isinstance(text,str):
                pay=payload['pagamentos'][index]
                if pay.get('condicao_pagamento')=='DINHEIRO': pay['declaracao_recebimento_dinheiro']=text.strip()
        cash_errors=policy.cash_receipt_errors(payload)
        if cash_errors: raise ValueError(' '.join(cash_errors))
        cash_receipts=[]
        for index,pay in enumerate(payload.get('pagamentos') or [],1):
            if pay.get('condicao_pagamento')=='DINHEIRO':
                receipt={'pagamento':index,'valor_centavos':pay.get('valor_centavos'),'declaracao':pay['declaracao_recebimento_dinheiro'],'operador':operador,'registrado_em':datetime.now(timezone.utc).isoformat()}
                pay['recebimento_dinheiro_auditoria']=receipt
                cash_receipts.append(receipt)
        config=payload.get("parametros_snapshot")
        if config:
            result=policy.evaluate(payload,config,con,exclude_id=os_id)
            reason=str(body.get("justificativa") or "").strip()
            if 'justifications' in config:
                for index,text in enumerate(body.get('justificativas_comprovantes') or []):
                    if index<len(payload.get('pagamentos') or []) and isinstance(text,str) and text.strip():
                        payload['pagamentos'][index]['justificativa_divergencia']=text.strip()
                if body.get('justificativa_financeira'): payload['justificativa_financeira']=str(body['justificativa_financeira']).strip()
                if reason: payload['justificativa_geral']=reason
                errors=policy.justification_errors(payload,config,payload.get('_other_changes',False))
                if errors: raise ValueError(' '.join(errors))
                reasons={'financeira':payload.get('justificativa_financeira') or '', 'geral':payload.get('justificativa_geral') or '',
                    'comprovantes':[{'pagamento':i,'forma':p.get('condicao_pagamento'),'justificativa':p['justificativa_divergencia']} for i,p in enumerate(payload.get('pagamentos') or [],1) if p.get('justificativa_divergencia')]}
            else:
                if config["approval_reason"] and len(reason)<10: raise ValueError("Justificativa da aprovação obrigatória (mínimo 10 caracteres).")
                reasons={'aprovacao':reason}
            reasons['dinheiro']=cash_receipts
            total=int(payload.get("total_centavos") or 0)
            paid=sum(int((p.get("comprovante_leitura") or {}).get("valor_centavos") or 0) if p.get("comprovante_arquivo") else int(p.get("valor_centavos") or 0) for p in payload.get("pagamentos",[]))
            con.execute("INSERT INTO liberacoes_administrativas(os_id,operador,status_anterior,divergencias_json,justificativas_json,total_centavos,comprovado_centavos,diferenca_centavos) VALUES(?,?,?,?,?,?,?,?)",
                (os_id,operador,osdata.get("status_conferencia"),json.dumps(result["reviews"],ensure_ascii=False),json.dumps(reasons,ensure_ascii=False),total,paid,paid-total if payload.get("pagamento_os")=="A_VISTA" else 0))
            con.execute('UPDATE os SET payload_json=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),os_id))
            con.execute("UPDATE os SET status_conferencia=? WHERE id=?",("LIBERADA_ADMINISTRACAO",os_id))
            return {"status":"LIBERADA_ADMINISTRACAO","divergencias":result["reviews"]}
        payments=payload.get("pagamentos") or []
        items=payload.get("itens") or []
        if not items: raise ValueError("OS sem serviços.")
        total=sum(int(it.get("preco_cobrado_centavos") or 0) for it in items)
        mode=str(osdata.get("pagamento_os") or "").upper()
        if mode not in ("FATURADO","A_VISTA"): raise ValueError("Condição de pagamento inválida.")
        if mode=="A_VISTA" and not payments: raise ValueError("OS à vista sem pagamentos.")
        comprovado=0; divergencias=[]; individuais=[]
        for i,p in enumerate(payments,1):
            forma=p.get("condicao_pagamento")
            if forma=="DINHEIRO":
                comprovado+=int(p.get("valor_centavos") or 0)
                continue
            if not p.get("comprovante_arquivo"): raise ValueError(f"Pagamento {i}: comprovante ausente.")
            leitura=p.get("comprovante_leitura") or {}
            val=leitura.get("valor_centavos")
            if val is None:
                divergencias.append(f"Pagamento {i}: valor do comprovante não identificado")
            else:
                comprovado+=int(val)
            validacao=leitura.get("validacao") or {}
            motivos=[]
            if val is not None and int(val)!=int(p.get("valor_centavos") or 0): motivos.append("Valor divergente")
            if validacao.get("methodOk") is False: motivos.append("Forma divergente")
            if validacao.get("timeOk") is False: motivos.append("Data/hora fora da janela")
            if validacao.get("valueOk") is False: motivos.append("Valor não validado")
            if validacao.get("needsReview") and not motivos: motivos.append("Revisão Vision")
            if motivos:
                divergencias.append(f"Pagamento {i}: "+", ".join(motivos))
                justificativa=str(p.get("justificativa_divergencia") or "").strip()
                if len(justificativa)<10: raise ValueError(f"Pagamento {i}: justificativa individual obrigatória.")
                individuais.append({"pagamento":i,"forma":forma,"justificativa":justificativa})
        diff=0 if mode=="FATURADO" else comprovado-total
        if mode!="FATURADO" and diff: divergencias.append("Conciliação: diferença de "+str(diff)+" centavos")
        ultima=con.execute("SELECT justificativa_financeira, justificativa FROM auditoria_os WHERE os_id=? ORDER BY id DESC LIMIT 1",(os_id,)).fetchone()
        fin=str(ultima["justificativa_financeira"] or "").strip() if ultima else ""
        if mode!="FATURADO" and (diff or any("não identificado" in d for d in divergencias)) and len(fin)<10:
            raise ValueError("Justificativa da conciliação financeira obrigatória na última edição.")
        justificativas={"financeira":fin,"comprovantes":individuais,"dinheiro":cash_receipts}
        con.execute("INSERT INTO liberacoes_administrativas(os_id,operador,status_anterior,divergencias_json,justificativas_json,total_centavos,comprovado_centavos,diferenca_centavos) VALUES(?,?,?,?,?,?,?,?)",
            (os_id,operador,osdata.get("status_conferencia"),json.dumps(divergencias,ensure_ascii=False),json.dumps(justificativas,ensure_ascii=False),total,comprovado,diff))
        con.execute("UPDATE os SET payload_json=? WHERE id=?",(json.dumps(payload,ensure_ascii=False),os_id))
        con.execute("UPDATE os SET status_conferencia=? WHERE id=?",("LIBERADA_ADMINISTRACAO",os_id))
        return {"status":"LIBERADA_ADMINISTRACAO","divergencias":divergencias}

def edit_os(body):
    operador=str(body.get("_operador_autenticado") or "").strip()
    if not operador: raise PermissionError("Sessão administrativa obrigatória.")
    justificativa=str(body.get("justificativa", "")).strip()
    financeira=str(body.get("justificativa_financeira", "")).strip()
    os_id=int(body.get("os_id"))
    campos=body.get("campos")
    itens=body.get("itens")
    pagamentos=body.get("pagamentos")
    documentos=body.get("documentos")
    if not isinstance(campos,dict) or set(campos)-set(EDITABLE_FIELDS): raise ValueError("Campo não editável enviado.")
    if not isinstance(itens,list) or not isinstance(pagamentos,list) or not isinstance(documentos,dict): raise ValueError("Estrutura de edição inválida.")
    if set(documentos)-{"CNH","CRLV","REQUISICAO"}: raise ValueError("Tipo de documento inválido.")
    if len(itens)>100 or len(pagamentos)>30: raise ValueError("Quantidade de itens ou pagamentos excedida.")
    cat=admin_catalog()
    for item in itens:
        if not isinstance(item,dict) or not str(item.get("servico","")).strip(): raise ValueError("Serviço inválido.")
        item["preco_cobrado_centavos"]=int(item.get("preco_cobrado_centavos",0))
        if item["preco_cobrado_centavos"]<0: raise ValueError("Preço negativo.")
    for p in pagamentos:
        if not isinstance(p,dict): raise ValueError("Pagamento inválido.")
        p["valor_centavos"]=int(p.get("valor_centavos",0))
        if p["valor_centavos"]<0: raise ValueError("Pagamento negativo.")
    total=sum(x["preco_cobrado_centavos"] for x in itens)
    # A conciliação é feita com os valores efetivamente identificados nos comprovantes ativos.
    # O valor declarado pelo operador é apenas informativo.
    # A leitura do comprovante original é resolvida e validada após carregar a OS.
    diferenca=0
    saved=[]
    from contextlib import closing
    with closing(db()) as con:
      con.execute("BEGIN IMMEDIATE")
      try:
        row=con.execute("SELECT * FROM os WHERE id=?",(os_id,)).fetchone()
        if not row: raise ValueError("OS não encontrada.")
        original=dict(row)
        if original.get("erp_id"): raise ValueError("OS já vinculada ao ERP: edição bloqueada até definirmos a política de sincronização.")
        old_payload=json.loads(original["payload_json"] or "{}")
        # Justificativa geral somente para mudanças não financeiras efetivas.
        nonfinancial_changed=any(
            str(v if v is not None else "")!=str(original.get(k,old_payload.get(k,"")) if original.get(k,old_payload.get(k,"")) is not None else "")
            for k,v in campos.items() if k not in ("pagamento_os",) and k!="cliente_nome"
        )
        old_docs_check=old_payload.get("documentos_arquivos") or {}
        nonfinancial_changed=nonfinancial_changed or any(
            (entry is None and bool(old_docs_check.get(kind))) or
            (isinstance(entry,str) and entry.startswith("data:"))
            for kind,entry in documentos.items()
        )
        reason_config=old_payload.get('parametros_snapshot') or {}
        if 'justifications' not in reason_config and nonfinancial_changed and len(justificativa)<10:
            raise ValueError("Justificativa geral obrigatória para alterações não financeiras (mínimo 10 caracteres).")
        if not justificativa and 'justifications' not in reason_config: justificativa="Revisão financeira sem alteração de dados não financeiros."

        client_id=campos.get("cliente_id") or original.get("cliente_id") or old_payload.get("cliente_id")
        client=next((c for c in cat["clients"] if c["id_interno"]==client_id),None)
        if not client: raise ValueError("Cliente não encontrado na base de preços do Pré-Laudo.")
        if campos.get("cliente_nome")!=client["name"]:
            raise ValueError("Cliente deve ser selecionado no catálogo do Pré-Laudo.")
        mode=str(campos.get("pagamento_os") or original.get("pagamento_os") or "").upper()
        if mode not in ("A_VISTA","FATURADO"):
            raise ValueError("Condição da OS inválida.")
        period=str(client.get("period") or "").strip().lower()
        if mode=="FATURADO" and period not in ("mensal","quinzenal"):
            raise ValueError("Cliente não habilitado para faturamento.")
        if campos.get("local_vistoria",original.get("local_vistoria")) not in ("Base","Móvel"):
            raise ValueError("Local da vistoria inválido.")
        if mode=="A_VISTA":
            if not pagamentos: raise ValueError("OS à vista exige pagamento informado.")
            for payment in pagamentos:
                if policy.payment_rule(old_payload.get("parametros_snapshot") or config_atual(),payment.get("condicao_pagamento"))["proof"] and not (payment.get("comprovante_imagem") or payment.get("comprovante_arquivo")):
                    raise ValueError("Pagamento à vista exige comprovante, exceto dinheiro.")

        for it in itens:
            reference=validate_catalog_item(it,client,cat)
            it["preco_referencia_centavos"]=reference
            it["preco_editado"]=int(it["preco_cobrado_centavos"]!=reference)

        new_payload=json.loads(json.dumps(old_payload,ensure_ascii=False))
        for k,v in campos.items():
            if not isinstance(v,(str,int,float,type(None))): raise ValueError("Valor de campo inválido.")
            new_payload[k]=v
        # Não permitir que a interface modifique identidade, unidade, vistoriador ou datas.
        for k in ("id_interno","unidade_id","unidade_nome","vistoriador","vistoriador_id","aberta_em","enviada_em"):
            if k in old_payload: new_payload[k]=old_payload[k]
        old_docs=old_payload.get("documentos_arquivos") or {}
        new_docs=dict(old_docs)
        for kind,entry in documentos.items():
            if entry is None: new_docs.pop(kind,None)
            elif isinstance(entry,str) and entry.startswith("data:"):
                rel=save_document(entry,original["id_interno"],kind)
                saved.append(rel);new_docs[kind]=rel
            elif entry!=old_docs.get(kind): raise ValueError("Documento não reconhecido.")
        new_payload["documentos_arquivos"]=new_docs
        old_pays=old_payload.get("pagamentos") or []
        # Recupera leituras originais tanto do payload quanto da tabela relacional.
        rows=con.execute("SELECT * FROM pagamentos WHERE os_id=? ORDER BY ordem,id",(os_id,)).fetchall()
        originals=[]
        for j,r in enumerate(rows):
            d=dict(r)
            p=dict(old_pays[j]) if j<len(old_pays) and isinstance(old_pays[j],dict) else {}
            try:
                stored=json.loads(d.get("pagamento_json") or "{}")
                if isinstance(stored,dict): p={**stored,**p}
            except (ValueError,TypeError): pass
            p.setdefault("comprovante_arquivo",d.get("comprovante_arquivo"))
            if not isinstance(p.get("comprovante_leitura"),dict) or p["comprovante_leitura"].get("valor_centavos") is None:
                if d.get("vision_valor_centavos") is not None:
                    p["comprovante_leitura"]={"valor_centavos":d["vision_valor_centavos"],"data_hora":d.get("vision_data_hora"),"id_transacao":d.get("identificador_transacao"),"condicao":d.get("vision_condicao"),"validacao":{"methodOk":bool(d.get("check_forma")),"valueOk":bool(d.get("check_valor")),"timeOk":bool(d.get("check_tempo")),"needsReview":bool(d.get("needs_review"))}}
            originals.append(p)
        by_file={p.get("comprovante_arquivo"):p for p in originals if p.get("comprovante_arquivo")}
        used_originals=set()
        new_pays=[]
        for i,p in enumerate(pagamentos,1):
            ref=p.get("comprovante_arquivo")
            old=by_file.get(ref) if ref else None
            if old is None and not ref and i<=len(originals) and not originals[i-1].get("comprovante_arquivo"):
                old=originals[i-1]
            old=old or {}
            if ref:
                if ref in used_originals: raise ValueError("Comprovante original duplicado em pagamentos distintos.")
                used_originals.add(ref)
            q={k:v for k,v in p.items() if k in ("condicao_pagamento","Id_CondPgtoERP","valor_centavos","cashCode","comprovante_nome","identificador_manual","declaracao_recebimento_dinheiro")}
            if "declaracao_recebimento_dinheiro" not in q and old.get("declaracao_recebimento_dinheiro"):
                q["declaracao_recebimento_dinheiro"]=old["declaracao_recebimento_dinheiro"]
            if not q.get("condicao_pagamento"): raise ValueError("Informe a condição de pagamento.")
            attachment=p.get("comprovante_imagem")
            if attachment:
                reading=p.get("comprovante_leitura") or {}
                if not reading.get("_admin_vision"):
                    raise ValueError("Novo comprovante exige leitura pelo Vision antes de salvar.")
                q["comprovante_leitura"]={k:v for k,v in reading.items() if k!="_admin_vision"}
                rel=save_data_url(attachment,original["id_interno"],i,q.get("comprovante_nome"))
                if not rel: raise ValueError("Comprovante inválido.")
                saved.append(rel);q["comprovante_arquivo"]=rel
            elif p.get("comprovante_arquivo"):
                if p["comprovante_arquivo"]!=old.get("comprovante_arquivo"): raise ValueError("Comprovante original inválido.")
                q["comprovante_arquivo"]=old["comprovante_arquivo"]
            # Dados do Vision são conservados somente se o comprovante não mudou.
            if q.get("comprovante_arquivo") and q.get("comprovante_arquivo")==old.get("comprovante_arquivo"):
                q["comprovante_leitura"]=old.get("comprovante_leitura")
            if p.get("justificativa_divergencia"):
                q["justificativa_divergencia"]=str(p["justificativa_divergencia"]).strip()
            if not old_payload.get("parametros_snapshot"): review_payment(q,old)
            new_pays.append(q)
        comprovado=0
        pendentes=0
        for pay in new_pays:
            if pay.get("condicao_pagamento")=="DINHEIRO":
                comprovado+=int(pay["valor_centavos"])
                continue
            leitura=pay.get("comprovante_leitura") or {}
            valor=leitura.get("valor_centavos")
            if valor is None:
                pendentes+=1
            else:
                try:
                    comprovado+=int(valor)
                except (ValueError,TypeError):
                    pendentes+=1
        diferenca=0 if mode=="FATURADO" else comprovado-total
        if not old_payload.get("parametros_snapshot") and mode!="FATURADO" and (diferenca or pendentes) and len(financeira)<10:
            raise ValueError("Conciliação por comprovantes divergente ou não validada: justificativa financeira obrigatória (mínimo 10 caracteres).")
        new_payload["itens"]=itens
        new_payload["pagamentos"]=new_pays
        new_payload["total_centavos"]=total
        if 'justifications' in reason_config:
            item_keys=('servico','servico_erp_id','preco_cobrado_centavos')
            item_changes=[{k:i.get(k) for k in item_keys} for i in itens]!=[{k:i.get(k) for k in item_keys} for i in old_payload.get('itens') or []]
            other_changed=nonfinancial_changed or item_changes
            new_payload['_other_changes']=bool(old_payload.get('_other_changes') or other_changed)
            new_payload['justificativa_geral']=justificativa or old_payload.get('justificativa_geral') or ''
            new_payload['justificativa_financeira']=financeira or old_payload.get('justificativa_financeira') or ''
            policy.evaluate(new_payload,reason_config,con,exclude_id=os_id)
            # Para uma nova alteração geral, uma justificativa anterior não a substitui.
            check_payload=dict(new_payload)
            if other_changed: check_payload['justificativa_geral']=justificativa
            errors=policy.justification_errors(check_payload,reason_config,other_changed)
            if errors: raise ValueError(' '.join(errors))
        changes=[]
        for k in EDITABLE_FIELDS:
            before=original.get(k)
            after=campos.get(k,before)
            if str(before or "")!=str(after or ""):
                changes.append({"campo":k,"antes":before,"depois":after})
        for k in ("itens","pagamentos","documentos_arquivos"):
            before=old_payload.get(k,[] if k!="documentos_arquivos" else {})
            after=new_payload.get(k)
            if before!=after: changes.append({"campo":k,"antes":before,"depois":after})
        if total!=int(original.get("total_centavos") or 0):
            changes.append({"campo":"total_centavos","antes":original.get("total_centavos"),"depois":total})
        if 'justifications' in reason_config:
            for key in ('justificativa_geral','justificativa_financeira'):
                if new_payload.get(key)!=old_payload.get(key): changes.append({'campo':key,'antes':old_payload.get(key),'depois':new_payload.get(key)})
        if not changes: raise ValueError("Nenhuma alteração detectada.")
        updates={k:campos[k] for k in EDITABLE_FIELDS if k in campos}
        updates["tabela_preco_indice"]=str(client["price_index"])
        new_payload["tabela_preco_indice"]=str(client["price_index"])
        updates["total_centavos"]=total
        if new_payload.get("parametros_snapshot"):
            policy.evaluate(new_payload,new_payload["parametros_snapshot"],con,exclude_id=os_id)
        novo_status,_=classificar_os(new_payload,mode)
        # A alteração segue o mesmo workflow da inclusão, sem liberação especial.
        updates["status_conferencia"]=novo_status
        new_payload["status_conferencia"]=novo_status
        updates["payload_json"]=json.dumps(new_payload,ensure_ascii=False)
        sql="UPDATE os SET "+", ".join(k+"=?" for k in updates)+" WHERE id=?"
        con.execute(sql,(*updates.values(),os_id))
        con.execute("DELETE FROM itens_os WHERE os_id=?",(os_id,))
        for it in itens:
            con.execute("""INSERT INTO itens_os(os_id,servico,servico_erp_id,preco_referencia_centavos,preco_cobrado_centavos,preco_origem,preco_editado,item_json) VALUES(?,?,?,?,?,?,?,?)""",
              (os_id,it.get("servico"),it.get("servico_erp_id"),it.get("preco_referencia_centavos"),it["preco_cobrado_centavos"],it.get("preco_origem"),it.get("preco_editado",0),json.dumps(it,ensure_ascii=False)))
        con.execute("DELETE FROM pagamentos WHERE os_id=?",(os_id,))
        policy.reserve_transactions(con,old_payload,os_id)
        policy.reserve_transactions(con,new_payload,os_id)
        for i,p in enumerate(new_pays,1):
            v=p.get("comprovante_leitura") or {}; val=v.get("validacao") or {}
            con.execute("""INSERT INTO pagamentos(os_id,ordem,condicao_pagamento,Id_CondPgtoERP,valor_centavos,cashCode,comprovante_nome,comprovante_anexado,comprovante_arquivo,vision_condicao,vision_valor_centavos,vision_data_hora,identificador_transacao,tipo_identificador,check_forma,check_valor,check_tempo,needs_review,pagamento_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (os_id,i,p.get("condicao_pagamento"),p.get("Id_CondPgtoERP"),p["valor_centavos"],p.get("cashCode"),p.get("comprovante_nome"),int(bool(p.get("comprovante_arquivo"))),p.get("comprovante_arquivo"),v.get("condicao"),v.get("valor_centavos"),v.get("data_hora"),v.get("id_transacao"),v.get("tipo_identificador"),int(bool(val.get("methodOk"))),int(bool(val.get("valueOk"))),int(bool(val.get("timeOk"))),int(bool(val.get("needsReview"))),json.dumps(p,ensure_ascii=False)))
        con.execute("INSERT INTO auditoria_os(os_id,operador,justificativa,justificativa_financeira,diferenca_centavos,alteracoes_json,anterior_json,novo_json) VALUES(?,?,?,?,?,?,?,?)",
            (os_id,operador,justificativa,financeira if (diferenca or pendentes) else None,diferenca,json.dumps(changes,ensure_ascii=False),json.dumps({"os":original,"payload":old_payload},ensure_ascii=False),json.dumps(new_payload,ensure_ascii=False)))
        con.commit()
        return {"alteracoes":len(changes),"diferenca_centavos":diferenca,"comprovantes_sem_valor":pendentes}
      except Exception:
        con.rollback()
        for rel in saved:
            try:(ROOT/rel).unlink(missing_ok=True)
            except OSError:pass
        raise

def config_atual():
    with db() as con: return policy.load(con)

classificar_os_legada = classificar_os
def classificar_os(payload, modo):
    config = payload.get("parametros_snapshot")
    if not config: return classificar_os_legada(payload, modo)
    result = policy.evaluate(payload, config, enforce=False)
    return result["status"], ("AGUARDANDO_LIBERACAO" if result["status"] == "PENDENTE" else "CONCLUIDA")

class Handler(SimpleHTTPRequestHandler):
    extensions_map={**SimpleHTTPRequestHandler.extensions_map,'.webmanifest':'application/manifest+json','.woff2':'font/woff2','.svg':'image/svg+xml'}
    def end_headers(self):
        self.send_header('Cache-Control','no-store, max-age=0')
        super().end_headers()
    def do_OPTIONS(self):
        path=urlparse(self.path).path
        if path not in ("/api/ler-comprovante", "/api/os", "/api/health", "/api/usuarios", "/api/catalogos", "/api/login", "/api/sessao", "/api/logout"):
            return json_response(self,404,{"ok":False,"erro":"Endpoint não encontrado"})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path=urlparse(self.path).path
        if path.rstrip("/").lower() in ("/admin","/administracao"):
            # Atalho curto para o Painel Administrativo.
            self.send_response(302);self.send_header("Location","/Camada_Intermediaria_v9346_Precos_ERP.html");self.send_header("Content-Length","0");self.end_headers();return
        if path in ("","/"):
            # A raiz do domínio abre o Pré-Laudo do vistoriador; o laboratório segue em /index.html.
            self.send_response(302);self.send_header("Location","/Pre_Laudo_Auto_Visao_v370_Precos_ERP.html");self.send_header("Content-Length","0");self.end_headers();return
        from urllib.parse import unquote
        import posixpath
        private_path=posixpath.normpath('/'+unquote(path).replace('\\','/').lstrip('/')).lower()
        if private_path in ('/work','/tests','/__pycache__') or private_path.startswith(("/work/", "/tests/", "/__pycache__/")):
            return json_response(self,403,{"ok":False,"erro":"Arquivo não publicado."})
        if path=="/api/admin/empresas":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            with db() as con: companies=empresas_erp.cached(con)
            return json_response(self,200,{"ok":True,"empresas":companies or [],"gravacao_multiempresa_validada":False})
        if path=="/api/catalogo/pagamentos":
            if not get_session(self) and not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Faça login."})
            with db() as con: state=pagamentos_erp.snapshot(con)
            return json_response(self,200,{"ok":True,"paymentConditions":admin_catalog()['paymentConditions'],"sincronizacao":state})
        if path=="/api/admin/transportadoras/sincronizacao":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            return json_response(self,200,{"ok":True,**carrier_sync().status()})
        if path=="/api/admin/pagamentos/sincronizacao":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            return json_response(self,200,{"ok":True,**payment_sync().status()})
        if path=="/api/catalogo/precos":
            if not get_session(self) and not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Faça login."})
            with db() as con: state=precos_erp.snapshot(con)
            return json_response(self,200,{"ok":True,"prices":admin_catalog()['prices'],"sincronizacao":state})
        if path=="/api/admin/precos/sincronizacao":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            with db() as con: state=precos_erp.snapshot(con) or {}
            shared=product_sync().status()
            return json_response(self,200,{"ok":True,**state,"configurado":shared['configurado'],"em_execucao":shared['em_execucao'],"ultimo_erro":shared['ultimo_erro'],"frequencia":shared['frequencia'],"total_erp":state.get('servicos_com_precos',0),"consulta_compartilhada_produtos":True})
        if path=="/api/catalogo/produtos":
            if not get_session(self) and not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Faça login."})
            with db() as con: state=produtos_erp.snapshot(con)
            return json_response(self,200,{"ok":True,"services":[p for p in admin_catalog()['services'] if p.get('selecionavel',True)],"sincronizacao":state})
        if path=="/api/admin/produtos/sincronizacao":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            return json_response(self,200,{"ok":True,**product_sync().status()})
        if path=="/api/catalogo/clientes":
            if not get_session(self) and not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Faça login."})
            with db() as con: state=clientes_erp.snapshot(con)
            return json_response(self,200,{"ok":True,"clients":[c for c in admin_catalog()['clients'] if c.get('ativo_erp',True)],"sincronizacao":state})
        if path=="/api/admin/clientes/sincronizacao":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            return json_response(self,200,{"ok":True,**client_sync().status()})
        if path=="/api/parametros":
            if not get_session(self) and not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Faça login."})
            return json_response(self,200,{"ok":True,"config":config_atual(),"erp_disponivel":False})
        if path=="/api/admin/parametros":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            return json_response(self,200,{"ok":True,"config":config_atual(),"erp_disponivel":False})
        # Bloqueia arquivos de dados, segredos e cópias de segurança.
        if private_path.endswith((".db",".sqlite",".sqlite3",".env",".py",".zip",".json",".db-wal",".db-shm",".bak",".dpapi")) or private_path in ("/config.env",) or "/.." in path:
            return json_response(self,403,{"ok":False,"erro":"Arquivo não publicado."})
        if path.startswith("/comprovantes/") and not admin_user(self):
            return json_response(self,403,{"ok":False,"erro":"Documento restrito à Administração."})
        if path=="/api/admin/integracoes":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            return json_response(self,200,{"ok":True,"integracoes":{n:integracao_status(n) for n in INTEGRACOES}})
        if path=="/api/admin/erp/acesso":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            return json_response(self,200,{"ok":True,"acesso":erp_acesso_status()})
        if path=="/api/admin/sessao":
            u=admin_user(self)
            return json_response(self,200 if u else 401,{"ok":bool(u),"usuario":u})
        if path=="/api/admin/catalogo":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            return json_response(self,200,{"ok":True,**admin_catalog()})
        if path=="/api/admin/usuarios":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            with db() as con:
                us=[dict(x) for x in con.execute("SELECT id,nome,login,ativo,criado_em FROM usuarios_administrativos ORDER BY nome")]
            return json_response(self,200,{"ok":True,"usuarios":us})
        if path=="/api/sessao":
            user=get_session(self)
            return json_response(self,200 if user else 401,{"ok":bool(user),"usuario":user})
        if path=="/api/catalogos":
            return json_response(self,200,{"ok":True,**catalogo()})
        if path=="/api/usuarios":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Acesso administrativo obrigatório."})
            return json_response(self,200,{"ok":True,"usuarios":list_users()})
        if path=="/api/health":
            return json_response(self,200,{"ok":True,"database":DB_PATH.name,"comprovantes":str(COMPROVANTES_DIR.name),"armazenamento_persistente":DATA_DIR!=ROOT and COMPROVANTES_DIR.resolve()==(DATA_DIR/"comprovantes").resolve() and os.access(COMPROVANTES_DIR,os.W_OK),"parametros_schema":3,"parametros_campos":sorted(policy.DEFAULTS),"servidor_build":"precos-erp-1"})
        if path=="/api/admin/os/historico":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            from urllib.parse import parse_qs
            try: os_id=int(parse_qs(urlparse(self.path).query).get("id",[0])[0])
            except (ValueError,TypeError): return json_response(self,400,{"ok":False,"erro":"ID inválido"})
            with db() as con:
                registros=[dict(r) for r in con.execute("SELECT id,registrado_em,operador,justificativa,justificativa_financeira,diferenca_centavos,alteracoes_json FROM auditoria_os WHERE os_id=? ORDER BY id DESC",(os_id,))]
                liberacoes=[dict(r) for r in con.execute("SELECT liberado_em,operador,status_anterior,divergencias_json,justificativas_json FROM liberacoes_administrativas WHERE os_id=? ORDER BY id DESC",(os_id,))]
            return json_response(self,200,{"ok":True,"registros":registros,"liberacoes":liberacoes})
        if path=="/api/admin/os":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            con=db()
            rows=[dict(r) for r in con.execute("SELECT * FROM os ORDER BY id DESC")]
            for osdata in rows:
                try:
                    reconciliar_status_os(con,osdata)
                except (ValueError,TypeError,KeyError,AttributeError,sqlite3.Error):
                    pass  # Cadastro incompleto: não promover a OS automaticamente.
            con.commit()
            for osdata in rows:
                osdata["etapa_analise_administrativa"]=etapa_analise_administrativa(con,osdata)
            con.close()
            return json_response(self,200,{"ok":True,"registros":rows})
        return super().do_GET()

    def _vision_admin(self,body):
        image=body.get("image")
        if not image:
            return json_response(self,400,{"ok":False,"erro":"Imagem ausente"})
        try:
            from openai import OpenAI
            cfg=integracao_config("vision")
            key=cfg["chave"]
            if not key: raise RuntimeError("Chave de IA não configurada. Cadastre em Administração > Integração IA.")
            model=cfg["modelo"]
            client=OpenAI(api_key=key)
            resp=client.responses.create(
                model=model,
                input=[{"role":"user","content":[
                    {"type":"input_text","text":PROMPT},
                    {"type":"input_image","image_url":image,"detail":"high"}
                ]}],
                max_output_tokens=500
            )
            txt=(resp.output_text or "").strip()
            txt=re.sub(r"^```(?:json)?\s*|\s*```$","",txt,flags=re.I|re.S)
            result=json.loads(txt)
            return json_response(self,200,{"ok":True,"resultado":result,"modelo":model})
        except Exception as e:
            return json_response(self,500,{"ok":False,"erro":str(e)})


    def do_POST(self):
        path=urlparse(self.path).path
        try:
            n=int(self.headers.get("Content-Length","0"))
            body=json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return json_response(self,400,{"ok":False,"erro":"JSON inválido: "+str(e)})

        if path=="/api/admin/transportadoras/sincronizar":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            if not clientes_erp.has_access(): return json_response(self,400,{"ok":False,"erro":"Configure o acesso à API do ERP antes de atualizar."})
            return json_response(self,202,{"ok":True,"iniciada":carrier_sync().trigger()})
        if path=="/api/admin/pagamentos/sincronizar":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            if not clientes_erp.has_access(): return json_response(self,400,{"ok":False,"erro":"Configure o acesso à API do ERP antes de atualizar."})
            return json_response(self,202,{"ok":True,"iniciada":payment_sync().trigger()})
        if path=="/api/admin/precos/sincronizar":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            if not clientes_erp.has_access(): return json_response(self,400,{"ok":False,"erro":"Configure o acesso à API do ERP antes de atualizar."})
            return json_response(self,202,{"ok":True,"iniciada":product_sync().trigger()})
        if path=="/api/admin/produtos/sincronizar":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            if not clientes_erp.has_access(): return json_response(self,400,{"ok":False,"erro":"Configure o acesso à API do ERP antes de atualizar."})
            return json_response(self,202,{"ok":True,"iniciada":product_sync().trigger()})
        if path=="/api/admin/clientes/sincronizar":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            if not clientes_erp.has_access(): return json_response(self,400,{"ok":False,"erro":"Configure o acesso à API do ERP antes de atualizar."})
            return json_response(self,202,{"ok":True,"iniciada":client_sync().trigger()})
        if path=="/api/admin/parametros":
            user=admin_user(self)
            if not user: return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            try:
                with db() as con:
                    config=policy.save(con,body.get("config"),user["login"])
                return json_response(self,200,{"ok":True,"config":config})
            except (ValueError,TypeError,KeyError) as e: return json_response(self,400,{"ok":False,"erro":str(e)})
        if path=="/api/os/validar":
            if not get_session(self): return json_response(self,401,{"ok":False,"erro":"Faça login."})
            try:
                with db() as con:
                    result=policy.evaluate(body,policy.load(con),con,catalog=admin_catalog(),enforce=False)
                return json_response(self,200,{"ok":True,**result})
            except (ValueError,TypeError,KeyError) as e: return json_response(self,400,{"ok":False,"erro":str(e)})
        if path=="/api/admin/login":
            login=str(body.get("login") or "").strip()
            pwd=str(body.get("senha") or "")
            with db() as con:
                u=con.execute("SELECT * FROM usuarios_administrativos WHERE login=? COLLATE NOCASE",(login,)).fetchone()
            if not u or not u["ativo"] or not verify_password(pwd,u["senha_hash"]):
                return json_response(self,401,{"ok":False,"erro":"Login ou senha inválidos."})
            token=secrets.token_urlsafe(40)
            ADMIN_SESSIONS[token]={"id":u["id"],"expires":datetime.now(timezone.utc)+timedelta(hours=8)}
            return json_response(self,200,{"ok":True,"token":token,"usuario":{"nome":u["nome"],"login":u["login"]}})
        if path in ("/api/admin/erp/acesso","/api/admin/erp/acesso/remover"):
            user=admin_user(self)
            if not user: return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            try:
                if path.endswith("/remover"): erp_acesso_remover()
                else: erp_acesso_salvar(body,user["login"])
                return json_response(self,200,{"ok":True,"acesso":erp_acesso_status()})
            except ValueError as e: return json_response(self,400,{"ok":False,"erro":str(e)})
        if path in ("/api/admin/integracoes","/api/admin/integracoes/remover","/api/admin/integracoes/testar"):
            user=admin_user(self)
            if not user: return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            nome=str(body.get("integracao") or "")
            try:
                if path.endswith("/testar"):
                    if nome!="vision": raise ValueError("Integração desconhecida.")
                    try: modelo=integracao_testar_vision()
                    except ValueError: raise
                    except Exception as e: return json_response(self,400,{"ok":False,"erro":"A OpenAI recusou a chave ou o modelo: "+str(e)[:300]})
                    return json_response(self,200,{"ok":True,"modelo":modelo})
                if path.endswith("/remover"): integracao_remover(nome)
                else: integracao_salvar(nome,body,user["login"])
                return json_response(self,200,{"ok":True,"integracao":integracao_status(nome)})
            except ValueError as e: return json_response(self,400,{"ok":False,"erro":str(e)})
        if path=="/api/admin/logout":
            auth=self.headers.get("Authorization","")
            ADMIN_SESSIONS.pop(auth[7:].strip() if auth.startswith("Bearer ") else "",None)
            return json_response(self,200,{"ok":True})
        if path=="/api/admin/usuarios":
            user=admin_user(self)
            if not user: return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            try:
                login=str(body.get("login") or "").strip().lower()
                nome=str(body.get("nome") or "").strip()
                senha=str(body.get("senha") or "")
                uid=body.get("id")
                if not re.fullmatch(r"[a-z0-9._-]{3,60}",login) or len(nome)<3: raise ValueError("Nome ou login inválido.")
                if senha and len(senha)<6: raise ValueError("Senha deve ter ao menos 6 caracteres.")
                if not uid and not senha: raise ValueError("Senha obrigatória para novo administrador.")
                with db() as con:
                    if uid:
                        if senha: con.execute("UPDATE usuarios_administrativos SET nome=?,login=?,ativo=?,senha_hash=? WHERE id=?",(nome,login,int(bool(body.get("ativo",True))),hash_password(senha),int(uid)))
                        else: con.execute("UPDATE usuarios_administrativos SET nome=?,login=?,ativo=? WHERE id=?",(nome,login,int(bool(body.get("ativo",True))),int(uid)))
                    else: con.execute("INSERT INTO usuarios_administrativos(nome,login,senha_hash,ativo) VALUES(?,?,?,?)",(nome,login,hash_password(senha),int(bool(body.get("ativo",True)))))
                return json_response(self,200,{"ok":True})
            except (ValueError,sqlite3.IntegrityError) as e: return json_response(self,400,{"ok":False,"erro":str(e)})
        if path=="/api/admin/ler-comprovante":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            # Reutiliza o mesmo motor Vision já usado pelo Pré-Laudo.
            self.path="/api/ler-comprovante"
            return self._vision_admin(body)
        if path=="/api/admin/os/liberar":
            user=admin_user(self)
            if not user: return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            body["_operador_autenticado"]=user["login"]
            try: return json_response(self,200,{"ok":True,**liberar_os_administrativamente(body)})
            except PermissionError as e: return json_response(self,403,{"ok":False,"erro":str(e)})
            except (ValueError,TypeError) as e: return json_response(self,400,{"ok":False,"erro":str(e)})
        if path=="/api/admin/os/editar":
            user=admin_user(self)
            if not user: return json_response(self,401,{"ok":False,"erro":"Login administrativo obrigatório."})
            body["_operador_autenticado"]=user["login"]
            try: return json_response(self,200,{"ok":True,**edit_os(body)})
            except PermissionError as e: return json_response(self,403,{"ok":False,"erro":str(e)})
            except (ValueError,TypeError) as e: return json_response(self,400,{"ok":False,"erro":str(e)})
            except sqlite3.OperationalError as e: return json_response(self,503,{"ok":False,"erro":str(e)})
            except Exception as e: return json_response(self,500,{"ok":False,"erro":"Erro ao salvar edição: "+str(e)})
        if path=="/api/login":
            login=str(body.get("login") or "").strip()
            password=str(body.get("senha") or "")
            result=authenticate(login,password) if login and password else None
            if not result: return json_response(self,401,{"ok":False,"erro":"Usuário ou senha inválidos, ou acesso bloqueado."})
            token,user=result
            return json_response(self,200,{"ok":True,"token":token,"usuario":user})
        if path=="/api/logout":
            header=self.headers.get("Authorization","")
            if header.startswith("Bearer "): SESSOES.pop(header[7:].strip(),None)
            return json_response(self,200,{"ok":True})
        if path=="/api/usuarios":
            if not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Acesso administrativo obrigatório."})
            try:
                editing=body.get("id") is not None
                login,vist,vnome,unidade,unome,pwd,ativo=validate_user(body,editing)
                with db() as con:
                    if editing:
                        uid=int(body["id"])
                        if not con.execute("SELECT 1 FROM usuarios_vistoriadores WHERE id=?",(uid,)).fetchone():
                            return json_response(self,404,{"ok":False,"erro":"Usuário não encontrado."})
                        if pwd:
                            con.execute("""UPDATE usuarios_vistoriadores SET login=?,vistoriador_id=?,vistoriador_nome=?,
                                unidade_id=?,unidade_nome=?,senha_hash=?,ativo=?,atualizado_em=CURRENT_TIMESTAMP WHERE id=?""",
                                (login,vist,vnome,unidade,unome,hash_password(pwd),ativo,uid))
                        else:
                            con.execute("""UPDATE usuarios_vistoriadores SET login=?,vistoriador_id=?,vistoriador_nome=?,
                                unidade_id=?,unidade_nome=?,ativo=?,atualizado_em=CURRENT_TIMESTAMP WHERE id=?""",
                                (login,vist,vnome,unidade,unome,ativo,uid))
                    else:
                        con.execute("""INSERT INTO usuarios_vistoriadores
                            (login,vistoriador_id,vistoriador_nome,unidade_id,unidade_nome,senha_hash,ativo)
                            VALUES (?,?,?,?,?,?,?)""",
                            (login,vist,vnome,unidade,unome,hash_password(pwd),ativo))
                return json_response(self,200,{"ok":True})
            except sqlite3.IntegrityError:
                return json_response(self,409,{"ok":False,"erro":"Login ou vistoriador já cadastrado. Vínculo 1:1 obrigatório."})
            except (ValueError,TypeError) as e:
                return json_response(self,400,{"ok":False,"erro":str(e)})
            except Exception:
                return json_response(self,500,{"ok":False,"erro":"Erro interno ao salvar usuário."})

        if path=="/api/ler-comprovante":
            # Exposto na internet: a leitura consome a chave OpenAI, então exige login.
            if not get_session(self) and not admin_user(self): return json_response(self,401,{"ok":False,"erro":"Faça login."})
            return self._vision_admin(body)

        if path=="/api/os":
            usuario=get_session(self)
            if not usuario: return json_response(self,401,{"ok":False,"erro":"Sessão inválida ou expirada. Faça login novamente."})
            # Identidade vem da sessão, nunca de valores manipuláveis no HTML.
            body["unidade_id"]=usuario["unidade_id"]
            body["unidade_nome"]=usuario["unidade_nome"]
            body["vistoriador"]=usuario["vistoriador_nome"]
            body["vistoriador_id"]=usuario["vistoriador_id"]
            body["vistoriador_erp_id"]=usuario.get("vistoriador_erp_id")
            try:
                required=["cliente_nome","placa","total_centavos"]
                missing=[k for k in required if body.get(k) in (None,"")]
                if missing:
                    return json_response(self,400,{"ok":False,"erro":"Campos obrigatórios ausentes: "+", ".join(missing)})

                pagamentos=body.get("pagamentos") or []
                with db() as con: payment_integrated=pagamentos_erp.cached(con) is not None
                if payment_integrated: pagamentos_erp.bind_payload(body,admin_catalog())

                for payment in pagamentos:
                    payment.pop("comprovante_arquivo",None)
                    payment.pop('justificativa_divergencia',None)
                    payment.pop('declaracao_recebimento_dinheiro',None)
                    payment.pop('recebimento_dinheiro_auditoria',None)
                for key in ('justificativa_financeira','justificativa_geral','_other_changes'):
                    body.pop(key,None)
                body.pop("documentos_arquivos",None)
                itens=body.get("itens") or []
                saved_paths=[]
                from contextlib import closing
                with closing(db()) as con:
                    # Serializa a alocação de números entre requisições concorrentes.
                    con.execute("BEGIN IMMEDIATE")
                    try:
                        body["parametros_snapshot"]=policy.load(con)
                        for payment in pagamentos:
                            if payment.get("identificador_manual") and not policy.payment_rule(body["parametros_snapshot"],payment.get("condicao_pagamento"))["proof"]:
                                reading=payment.setdefault("comprovante_leitura",{}) or {}
                                reading["id_transacao"]=payment["identificador_manual"]
                                payment["comprovante_leitura"]=reading
                        policy.evaluate(body,body["parametros_snapshot"],con,catalog=admin_catalog())
                        pricing_catalog=admin_catalog()
                        pricing_client=next(c for c in pricing_catalog['clients'] if c['id_interno']==body['cliente_id'])
                        body['tabela_preco_erp_id']=str(pricing_client.get('price_index') or 0)
                        body['tabela_preco_indice']=pricing_client.get('price_index') or 0
                        price_state=precos_erp.snapshot(con)
                        if price_state:
                            body['precos_snapshot']={'atualizado_em':price_state['ultima_atualizacao'],'numero_tabela_cliente':pricing_client.get('price_index') or 0,'origem':'ERPFlex variantes.precos'}
                            for price_item in body.get('itens',[]):
                                svc=next(s for s in pricing_catalog['services'] if (price_item.get('servico_erp_id') and str(price_item['servico_erp_id']) in (str(s.get('erp_id')),str(s.get('id_interno')),str(s.get('code')))) or (not price_item.get('servico_erp_id') and s.get('name')==price_item.get('servico')))
                                detail=precos_erp.detail(pricing_catalog['prices'][svc['code']],pricing_client)
                                price_item['preco_erp_snapshot']=detail
                                price_item['preco_referencia_centavos']=detail['cents']

                        # O identificador é atribuído pelo servidor, não pelo contador do HTML.
                        existing=[r[0] for r in con.execute("SELECT id_interno FROM os WHERE id_interno LIKE 'OS-%'")]
                        numeric=[int(x[3:]) for x in existing if x and re.fullmatch(r"OS-[0-9]+",x)]
                        last_db=con.execute("SELECT COALESCE(MAX(id),0) FROM os").fetchone()[0]
                        next_number=max([10000+int(last_db),10000]+numeric)+1
                        body["id_interno"]=f"OS-{next_number}"
                        for i,p in enumerate(pagamentos,1):
                            img=p.pop("comprovante_imagem",None)
                            if img:
                                p["comprovante_arquivo"]=save_data_url(img,body["id_interno"],i,p.get("comprovante_nome"))
                                saved_paths.append(p["comprovante_arquivo"])
                        documentos=body.pop("documentos",{}) or {}
                        if not isinstance(documentos,dict): raise ValueError("Documentos inválidos")
                        body["documentos_arquivos"]={}
                        for kind in ("CNH","CRLV","REQUISICAO"):
                            if documentos.get(kind):
                                rel=save_document(documentos[kind],body["id_interno"],kind)
                                body["documentos_arquivos"][kind]=rel
                                saved_paths.append(rel)
                        # Status calculado no servidor, jamais informado pelo cliente.
                        body["status_conferencia"],_=classificar_os(body,str(body.get("pagamento_os") or "").upper())
                        clean_body=json.loads(json.dumps(body,ensure_ascii=False))
                        cur=con.execute("""
                        INSERT INTO os (
                          id_interno,erp_id,aberta_em,enviada_em,unidade_id,unidade_nome,vistoriador,
                          cliente_id,cliente_erp_id,cliente_nome,placa,marca,modelo,local_vistoria,
                          condutor,cpf,email,celular,requisicao_cliente,pagamento_os,status_pagamento,
                          periodicidade_cliente,tabela_preco_indice,tabela_preco_erp_id,
                          condicao_pagamento,Id_CondPgtoERP,total_centavos,status_conferencia,payload_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,(
                          body.get("id_interno"),body.get("erp_id"),body.get("aberta_em"),body.get("enviada_em"),
                          body.get("unidade_id"),body.get("unidade_nome"),body.get("vistoriador"),
                          body.get("cliente_id"),body.get("cliente_erp_id"),body.get("cliente_nome"),
                          body.get("placa"),body.get("marca"),body.get("modelo"),body.get("local_vistoria"),
                          body.get("condutor"),body.get("cpf"),body.get("email"),body.get("celular"),
                          body.get("requisicao_cliente"),body.get("pagamento_os"),body.get("status_pagamento"),
                          body.get("periodicidade_cliente"),str(body.get("tabela_preco_indice") or ""),
                          body.get("tabela_preco_erp_id"),body.get("condicao_pagamento"),body.get("Id_CondPgtoERP"),
                          int(body.get("total_centavos") or 0),body.get("status_conferencia"),
                          json.dumps(clean_body,ensure_ascii=False)
                        ))
                        os_db_id=cur.lastrowid
                        policy.reserve_transactions(con,body,os_db_id)

                        for it in itens:
                            con.execute("""INSERT INTO itens_os
                            (os_id,servico,servico_erp_id,preco_referencia_centavos,preco_cobrado_centavos,
                             preco_origem,preco_editado,item_json) VALUES (?,?,?,?,?,?,?,?)""",(
                              os_db_id,it.get("servico"),it.get("servico_erp_id"),
                              it.get("preco_referencia_centavos"),it.get("preco_cobrado_centavos"),
                              it.get("preco_origem"),1 if it.get("preco_editado") else 0,
                              json.dumps(it,ensure_ascii=False)
                            ))

                        for i,p in enumerate(pagamentos,1):
                            vr=p.get("comprovante_leitura") or {}
                            val=vr.get("validacao") or {}
                            con.execute("""INSERT INTO pagamentos
                            (os_id,ordem,condicao_pagamento,Id_CondPgtoERP,valor_centavos,validated,cashCode,
                             comprovante_nome,comprovante_anexado,comprovante_arquivo,vision_condicao,
                             vision_valor_centavos,vision_data_hora,identificador_transacao,tipo_identificador,
                             check_forma,check_valor,check_tempo,needs_review,pagamento_json)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                              os_db_id,i,p.get("condicao_pagamento"),p.get("Id_CondPgtoERP"),p.get("valor_centavos"),
                              1 if p.get("validated") else 0,p.get("cashCode"),p.get("comprovante_nome"),
                              1 if p.get("comprovante_anexado") else 0,p.get("comprovante_arquivo"),
                              vr.get("condicao"),vr.get("valor_centavos"),vr.get("data_hora"),
                              vr.get("id_transacao"),vr.get("tipo_identificador"),
                              1 if val.get("methodOk") else 0,1 if val.get("valueOk") else 0,
                              1 if val.get("timeOk") else 0,1 if val.get("needsReview") else 0,
                              json.dumps(p,ensure_ascii=False)
                            ))
                        con.commit()
                    except Exception:
                        con.rollback()
                        for rel in saved_paths:
                            try: (ROOT / rel).unlink(missing_ok=True)
                            except OSError: pass
                        raise
                return json_response(self,200,{"ok":True,"registro":{
                    "id":os_db_id,"id_interno":body["id_interno"],
                    "status_conferencia":body.get("status_conferencia"),
                    "comprovantes_salvos":saved_paths
                }})
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    return json_response(self,503,{"ok":False,"erro":"Banco SQLite ocupado. Feche o DB Browser e outros programas que estejam editando o banco; aguarde e tente novamente."})
                return json_response(self,500,{"ok":False,"erro":str(e)})
            except ValueError as e:
                return json_response(self,400,{"ok":False,"erro":str(e)})
            except sqlite3.IntegrityError as e:
                return json_response(self,409,{"ok":False,"erro":"OS já existente ou conflito de integridade: "+str(e)})
            except Exception as e:
                return json_response(self,500,{"ok":False,"erro":str(e)})

        return json_response(self,404,{"ok":False,"erro":"Endpoint não encontrado"})

class ExclusiveHTTPServer(ThreadingHTTPServer):
    # No Windows, SO_REUSEADDR permite dois servidores diferentes na mesma porta.
    allow_reuse_address = os.name != 'nt'
    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()

def ensure_initial_admin():
    """Cria o primeiro administrador a partir de ADMIN_LOGIN/ADMIN_PASSWORD, somente se não houver nenhum."""
    login=(os.environ.get("ADMIN_LOGIN") or "").strip().lower()
    senha=os.environ.get("ADMIN_PASSWORD") or ""
    if not login or len(senha)<6: return
    with db() as con:
        if con.execute("SELECT 1 FROM usuarios_administrativos LIMIT 1").fetchone(): return
        con.execute("INSERT INTO usuarios_administrativos(nome,login,senha_hash,ativo) VALUES(?,?,?,1)",("Administrador",login,hash_password(senha)))
    print("Administrador inicial criado:",login)

def main(on_ready=None):
    host=os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    port=int(os.environ.get("PORT") or 8765)
    try:
        http=ExclusiveHTTPServer((host,port),Handler)
    except OSError as error:
        if getattr(error,'winerror',None) in (10048,10013) or getattr(error,'errno',None) in (98,48):
            print('Nao foi possivel iniciar: a porta 8765 ja esta ocupada ou indisponivel.')
            print('Se o Pre-Laudo ja estiver aberto, utilize a instancia existente.')
            return 1
        raise
    try:
        init_db()
        ensure_initial_admin()
    except Exception:
        http.server_close()
        raise
    print("Pré-Laudo / Vision / SQLite")
    print(f"Servidor: http://{host}:{port}/")
    print("Banco:",DB_PATH)
    print("Comprovantes:",COMPROVANTES_DIR)
    try:
        client_sync().start()
        product_sync().start()
        payment_sync().start()
        carrier_sync().start()
        if on_ready: on_ready()
        http.serve_forever()
    finally:
        if CLIENT_SYNC: CLIENT_SYNC.stop_event.set()
        if PRODUCT_SYNC: PRODUCT_SYNC.stop_event.set()
        if PAYMENT_SYNC: PAYMENT_SYNC.stop_event.set()
        if CARRIER_SYNC: CARRIER_SYNC.stop_event.set()
        http.server_close()
    return 0

if __name__=="__main__":
    raise SystemExit(main())
