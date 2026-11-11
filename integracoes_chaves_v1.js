/* Administração — Integração IA e Integração Vision OCR: cadastro das chaves.
   A chave é enviada ao servidor e nunca volta inteira para a tela. */
(function(){
  const PAGINAS={'Integração Vision OCR':'vision','Integração IA':'ia'};
  const el=(tag,props,...kids)=>{const n=document.createElement(tag);Object.assign(n,props||{});kids.forEach(k=>n.append(k));return n};
  function campo(id,rotulo,input){return el('div',{className:'user-field'},el('label',{htmlFor:id,textContent:rotulo}),input)}
  function montar(nome){
    const sec=document.querySelector('section.page[data-integracao="'+nome+'"]');if(!sec||sec.dataset.pronto)return sec;
    const vision=nome==='vision';
    const estado=el('div',{className:'integ-status',id:'integ-status-'+nome});
    const chave=el('input',{id:'integ-chave-'+nome,type:'password',autocomplete:'new-password',spellcheck:false,placeholder:'Cole a chave aqui'});
    const modelo=el('input',{id:'integ-modelo-'+nome,type:'text',autocomplete:'off',spellcheck:false,placeholder:vision?'gpt-5.5':'Ex.: nome do modelo'});
    const campos=el('div',{className:'user-grid'});
    let provedor=null;
    if(!vision){provedor=el('select',{id:'integ-provedor-'+nome});['','OpenAI','Anthropic (Claude)','Google (Gemini)','Outro'].forEach(v=>provedor.append(el('option',{value:v,textContent:v||'Selecione o provedor'})));campos.append(campo(provedor.id,'Provedor',provedor))}
    campos.append(campo(chave.id,'Chave de API',chave),campo(modelo.id,'Modelo',modelo));
    const msg=el('div',{className:'feedback',role:'status'});
    const salvar=el('button',{type:'submit',className:'primary',textContent:'Salvar chave'});
    const remover=el('button',{type:'button',className:'secondary',textContent:'Remover chave'});
    const acoes=el('div',{className:'integ-actions'},remover);
    let testar=null;
    if(vision){testar=el('button',{type:'button',className:'secondary',textContent:'Testar conexão'});acoes.append(testar)}
    acoes.append(salvar);
    const form=el('form',{className:'integ-form'},campos,el('small',{className:'integ-help',textContent:'Deixe o campo da chave vazio para manter a chave atual e alterar só o modelo.'}),acoes,msg);
    const aviso=(texto,tipo)=>{msg.textContent=texto;msg.className='feedback'+(tipo?' '+tipo:'')};
    const ocupar=on=>[salvar,remover,testar].forEach(b=>{if(b)b.disabled=on});
    async function enviar(caminho,corpo){const r=await fetch(API+caminho,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(corpo)});const d=await r.json();if(!r.ok||!d.ok)throw Error(d.erro||'Erro de comunicação');return d}
    form.addEventListener('submit',async e=>{e.preventDefault();ocupar(true);aviso('Salvando...');
      try{await enviar('/api/admin/integracoes',{integracao:nome,chave:chave.value,modelo:modelo.value,provedor:provedor?provedor.value:''});chave.value='';await carregar(nome);aviso('Configuração salva.','ok')}
      catch(err){aviso(err.message,'error')}finally{ocupar(false)}});
    remover.addEventListener('click',async()=>{if(!confirm('Remover a chave cadastrada no painel?'))return;ocupar(true);
      try{await enviar('/api/admin/integracoes/remover',{integracao:nome});await carregar(nome);aviso('Chave removida do painel.','ok')}
      catch(err){aviso(err.message,'error')}finally{ocupar(false)}});
    if(testar)testar.addEventListener('click',async()=>{ocupar(true);aviso('Testando a chave em uso...');
      try{const d=await enviar('/api/admin/integracoes/testar',{integracao:nome});aviso('Conexão confirmada. Modelo '+d.modelo+' disponível.','ok')}
      catch(err){aviso(err.message,'error')}finally{ocupar(false)}});
    sec.querySelector('.integ-mount').append(estado,form);sec.dataset.pronto='1';return sec;
  }
  async function carregar(nome){
    montar(nome);const estado=document.getElementById('integ-status-'+nome);if(!estado)return;
    try{
      const r=await fetch(API+'/api/admin/integracoes');const d=await r.json();if(!r.ok||!d.ok)throw Error(d.erro||'Erro de comunicação');
      const i=d.integracoes[nome];estado.replaceChildren();
      const tag=el('span',{className:'tag '+(i.configurada?'on':'off'),textContent:i.configurada?'Chave configurada':'Sem chave'});
      const partes=[];
      if(i.configurada){partes.push(i.final_chave?'final ••••'+i.final_chave:'chave cadastrada');partes.push(i.origem==='painel'?'cadastrada neste painel'+(i.operador?' por '+i.operador:''):'vinda da configuração do servidor')}
      if(i.modelo)partes.push('modelo '+i.modelo);
      estado.append(tag,el('span',{className:'integ-detail',textContent:partes.join(' · ')}));
      const modelo=document.getElementById('integ-modelo-'+nome);if(modelo&&!modelo.value)modelo.value=i.modelo||'';
      const provedor=document.getElementById('integ-provedor-'+nome);if(provedor&&!provedor.value&&i.provedor)provedor.value=i.provedor;
    }catch(err){estado.replaceChildren(el('span',{className:'tag off',textContent:'Não foi possível consultar'}),el('span',{className:'integ-detail',textContent:err.message}))}
  }
  document.addEventListener('click',e=>{const b=e.target.closest&&e.target.closest('button.nav');if(b&&PAGINAS[b.dataset.page])carregar(PAGINAS[b.dataset.page])});
})();
