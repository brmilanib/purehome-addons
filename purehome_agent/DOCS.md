# PureHome Agent

Conecta este Home Assistant ao **Portal PureHome**. Com ele, a equipe PureHome (e a sua revenda) acompanha a saúde do
equipamento e faz backup, atualização e busca de dispositivos à distância.

O agente **só faz conexões de saída** (HTTPS para `www.purehome.com.br`). Não é preciso abrir porta no roteador,
configurar túnel nem acesso externo.

## Instalação (5 minutos)

1. No **Portal PureHome**, abra a instalação do cliente e clique em **Copiar** ao lado do **código de ativação**
   (formato `PH-XXXX-XXXX`, vale 7 dias e uma vez só).
2. No Home Assistant do cliente: **Configurações → Add-ons → Loja de add-ons**.
3. Menu **⋮** (canto superior direito) → **Repositórios** → cole
   `https://github.com/brmilanib/purehome-addons` → **Adicionar** → **Fechar**.
4. Recarregue a página da loja, procure **PureHome Agent** e clique em **Instalar**.
5. Aba **Configuração**: cole o código em **Código de ativação** e clique em **Salvar**.
   Deixe o **Endereço do portal** como está.
6. Aba **Informações**: deixe **Iniciar na inicialização** ligado e clique em **Iniciar**.
7. Em até 1 minuto a instalação aparece como **Online** no portal. Na aba **Log** do add-on deve aparecer
   `Equipamento conectado ao portal (instalação #...)`.

Depois do primeiro contato o código não é mais usado e pode ficar na configuração.

## O que o portal consegue fazer

| Botão no portal | O que acontece neste Home Assistant |
|---|---|
| Backup agora | Cria um backup completo nos destinos já configurados em Configurações → Sistema → Backups (ex.: Google Drive e local). |
| Atualizar sistema | Se houver versão nova do Home Assistant Core, faz um backup antes e atualiza. Não atualiza o sistema operacional nem o Supervisor. |
| Buscar dispositivos | Lista as integrações que o Home Assistant descobriu na rede e aguardam configuração. |
| Aplicar configuração PureHome | Ainda não disponível nesta versão (o portal mostra erro, é esperado). |

A cada 60 s o agente envia: uso de CPU, memória e disco, versão do Home Assistant, IP local, data do último backup e
há quanto tempo o equipamento está ligado.

## Problemas comuns

| Mensagem no Log | O que fazer |
|---|---|
| `Sem código de ativação` | Cole o código na aba Configuração, salve e reinicie o add-on. |
| `Código de ativação inválido` / `expirado` / `já foi usado` | No portal, clique em **Gerar novo código**, cole o novo, salve e reinicie. |
| `Agente revogado — informe novo código de ativação` | Alguém revogou este equipamento no portal. Gere um novo código, cole, salve e reinicie. |
| `Acesso bloqueado antes de chegar ao portal (proteção da Vercel?)` | Só em testes da PureHome contra um preview: confira o Endereço do portal e a Chave de teste do preview. |
| `Portal indisponível ... Nova tentativa em ...` | Sem internet ou portal fora do ar. O agente tenta de novo sozinho (até a cada 5 min). |

Para trocar o equipamento de instalação: no portal, **Revogar agente**, e repita a partir do passo 5 com o novo código.

## Privacidade

O agente não envia senhas, nomes de usuários, câmeras nem histórico da casa. O token de acesso ao portal fica só em
`/data/agent.json` (dentro do add-on) e nunca aparece no log.
