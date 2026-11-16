# Changelog

## 1.0.0

- Primeira versão: registro pelo código de ativação, heartbeat (CPU, memória, disco, versão, IP, último backup),
  tarefas do portal (backup, atualizar Core, buscar dispositivos). "Aplicar configuração PureHome" ainda não disponível.
- Imagem base Python 3.13 (Alpine 3.22) para amd64 e aarch64.
- Opção "Chave de teste do preview" (só para testes contra preview protegido da Vercel). Bloqueio da Vercel não é tratado como revogação.
