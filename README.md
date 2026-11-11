# Pré-Laudo Auto Visão

Código do Pré-Laudo do Vistoriador e da Camada Intermediária. Este pacote contém catálogos vazios. A operação precisa configurar os cadastros e usuários no ambiente de destino.

## Execução local

Instale Python e as dependências de requirements.txt. Copie config.env.example para config.env e configure a chave OpenAI nesse arquivo. Execute INICIAR.bat no Windows ou python server.py para iniciar o servidor local. A aplicação usa a porta 8765.

## Hospedagem no Railway

A aplicação está publicada no Railway a partir do `Dockerfile`, em `os.autovisao.com`. A raiz abre o Pré-Laudo do vistoriador e `/admin` abre o Painel Administrativo.

Variáveis do serviço:

- `PORT`: porta do servidor. Com ela definida, o servidor aceita conexões externas.
- `DATA_DIR`: pasta do volume persistente (banco SQLite, comprovantes e catálogos). Sem ela, tudo fica na pasta do programa.
- `ADMIN_LOGIN` e `ADMIN_PASSWORD`: criam o primeiro administrador, somente quando ainda não existe nenhum.
- `ERPFLEX_USERNAME` e `ERPFLEX_PASSWORD`: acesso à API do ERPFlex para as cargas de clientes, produtos, pagamentos, vistoriadores e preços. Com elas definidas, as cargas diárias e os botões da Integração ERP funcionam na hospedagem. O usuário e a senha também podem ser cadastrados em Administração > Integração ERP, e esse cadastro tem prioridade sobre as variáveis.
- `OPENAI_API_KEY` e `OPENAI_MODEL`: chave e modelo da leitura de comprovantes. A chave cadastrada em Administração > Integração IA tem prioridade.

Na hospedagem, liberar OS, editar OS e ver o histórico ficam disponíveis a qualquer administrador logado, e a leitura de comprovantes exige login. No Windows, o acesso ao ERPFlex continua podendo ser guardado pela proteção de credenciais do sistema; as variáveis de ambiente têm prioridade.

## Implantação

O servidor atual usa Python HTTPServer, SQLite local e arquivos locais para comprovantes. O acesso ERP salvo localmente usa proteção do Windows. Para hospedar na Vercel e usar Supabase, adaptar as rotas do servidor, persistência em Postgres, armazenamento privado dos comprovantes e configuração de credenciais e sessões. Validar autenticação e fluxos antes de apontar o domínio de produção. A gravação de orçamento por unidade no ERP ainda depende da orientação do fornecedor.

Configurar os segredos no ambiente de hospedagem. A migração de dados deve ocorrer por canal privado, com acesso restrito.
