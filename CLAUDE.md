# Contexto do projeto

## Quem sou eu
Engenheiro com background em investimento quantitativo (hedge fund sistemático), Python, dados e IA. Trabalho hoje na FactSet (dados financeiros); meu contrato permite atividades extras. Não tenho audiência nem canal de vendas, e quero permanecer anônimo (sem marca pessoal; distribuição via produto, SEO, marketplace ou parceria).

## Objetivo
Encontrar e lançar um negócio online (produto ou serviço recorrente) fora do trading, com preferência por poucos clientes diretos e pouca dependência de atendimento. Estratégia central: identificar modelos que já funcionam nos EUA e ainda não existem (ou estão fracos) no Brasil, e comercializá-los aqui.
- Meta mínima de sucesso: ~R$ 5 mil/mês.
- Meta de longo prazo: construir algo com teto alto (equity), não só renda extra.
- Horas por semana: flexível, aumento conforme a promessa da oportunidade. Não trate tempo como restrição.
- Decisões anteriores: trading sistemático com ~R$ 50 mil foi avaliado e descartado como fonte principal (retorno esperado baixo vs. tempo).

## Projeto atual: pipeline de pesquisa de ideias
Pipeline em Python que coleta conteúdo de empreendedorismo online (YouTube primeiro), filtra, extrai negócios de forma estruturada e gera um ranking.
- Funil: Camada 0 (metadados, sem LLM) → baixar legendas dos sobreviventes → Camada 1 (Haiku, triagem) → Camada 2 (Sonnet 5.5, extração completa).
- Discover: em duas etapas: (a) triagem por título/canal/duração/views com Camada 0 + Haiku, sem baixar legenda; (b) só os sobreviventes baixam legenda e passam pela Camada 1.
- Extração: campos numéricos exigem supporting_quote verificada em código (match aproximado); evidence_quality atribuído pelo modelo (shown é raro, não temos o vídeo).
- Dedupe: fastembed (bge-small-en-v1.5) + rapidfuzz; ambíguos vão para revisão manual.
- Ranking: scoring.yaml com pesos ajustáveis, incluindo fator por evidence_quality.
- Fora do escopo por ora: Reddit, Indie Hackers, listagens de empresas à venda. Próximas fontes planejadas (verificar termos/acesso antes): Hacker News API, Reddit API oficial, RSS/newsletters, Product Hunt API. Para isso, refatorar para um formato unificado de documento: {source, source_id, url, date, author, title, text, engagement}.
- Etapa final importante: validar no Brasil os top 15-20 do ranking (concorrentes em português, Google Trends, regulação, viabilidade de pagamento/impostos) para separar "funciona nos EUA" de "tem espaço aqui".

## Regras de trabalho
- Conversa em português; código, nomes, commits e prompts enviados à API em inglês.
- Tudo configurável por YAML, retomável, com cache; nada reprocessado sem necessidade.
- Antes de qualquer chamada paga em volume: mostrar estimativa de custo e esperar confirmação (--yes). Preços ficam em config/pricing.yaml; recuse rodar se houver placeholder "VERIFICAR".
- Não inventar dados; ausente = null. Palpites sobre o mercado brasileiro devem ser marcados como hipótese a verificar.
- Seja realista e direto, sem otimismo exagerado. Aponte vieses (sobrevivência, receita alegada vs. verificada).
- Quando uma decisão for minha (escopo, custo, direção), pergunte antes de executar.

## Estado atual
- Etapa 1 (download) funciona. Etapas 2/3 estão sendo construídas do zero seguindo o plano acordado: llm.py, filters.py, triage.py, extract.py, discover.py, snowball.py, dedupe.py, deep_dive.py, rank.py, cli.py.
- Sequência de entrega: (1) llm.py + Camada 0 + relatório do funil em amostra de ~40 vídeos; (2) Camada 1 nessa amostra, depois PARAR para eu validar os cortes; (3) extract, dedupe, rank, deep_dive; (4) discover e snowball.
- Passos (1) e (2) construídos (commit 6d7feaa): llm.py, filters.py, triage.py, cli.py (layer0, triage, funnel). Camada 0 testada nos 5 canais; amostra de 40 vídeos baixada (starter-story e my-first-million). Camada 1 AINDA NÃO rodou: falta preencher config/pricing.yaml e definir ANTHROPIC_API_KEY. Aguardando validação dos cortes antes do passo (3).
- Fontes: formato unificado de documento (src/documents.py) feito; YouTube lido on the fly de data/transcripts. Hacker News conectado (src/hackernews.py, config/sources.yaml); 40 posts baixados, triagem deles NÃO rodada (estimativa ~US$ 0,004). Product Hunt e Reddit: termos verificados em 2026-11-08, não construídos (PH: API proíbe uso comercial e raspagem; Reddit: exige aprovação, acesso novo possivelmente fechado). Extract segue em espera por decisão do usuário.
- Canais: 10 no channels.yaml (5 originais + top-founders-latka, koerner-office, florian-darroman, saas-club, niche-pursuits; 3 vídeos de cada baixados). Filtros Layer 0: views OFF (views_percentile: 0), títulos ON, duração ON. Suporte a idiomas por canal (languages) implementado e testado em ja/fr/en; candidatos não adicionados ainda: @TheNumbersGame1, @HenrySaaS, @NitinBajajMOG, @TheoClarkePodcast, @PracticalFounders, @AbderahmanEcom (fr), @bubecode (ja). Bug corrigido: legendas automáticas "-orig" não são o original; só aceitar se o idioma do vídeo bater.
- Limitação conhecida: a listagem rápida do yt-dlp não traz data de upload, então views_per_day cai para views brutas até haver metadados completos.
- Camada 2 começa com Sonnet 5.5; comparar com Opus 5.5 em ~20 vídeos antes de decidir.
- Mantenha esta seção atualizada quando uma etapa for concluída ou uma decisão importante for tomada.
