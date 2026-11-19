# Equações históricas de PFTHM por COD

Versão dos dados: `2026-11-17-v4-ipt-cod`.
Fonte: RT IPT 125967-205, seção 3, tabela 1, linhas 1 e 2, página 13 do PDF
(10/16 do relatório). As equações foram originalmente ajustadas em dois pontos.
A implementação calcula **ambas em todos os pontos com COD exato**, por solicitação
do usuário. Aplicação fora do ponto original é **extrapolação experimental não
validada**, não uma ampliação demonstrada do domínio do modelo.

| Ponto | Equação de PFTHM (µg/L) | R² do ajuste original |
|---|---|---:|
| ANOR02300 | 235 + 15,9 × COD + 282 × JFM | 81,5% |
| ARPE02800 | −1983 + 669 × COD − 43,3 × COD² | 62,0% |

COD é carbono orgânico dissolvido em mg/L. JFM vale 1 em janeiro–março e 0
nos demais meses. O termo escrito `DOC²` na segunda equação foi interpretado
como `COD²` (sigla inglesa do mesmo analito); essa interpretação está explícita
no código e ainda deve ser conferida com a base original do estudo.

Não se exige UV254. Não há substituição por COT, recalibração dos coeficientes
ou cálculo de probabilidades a partir de R²/S. A transferência entre pontos
está identificada explicitamente nas saídas e no mapa.
O intervalo de calibração de COD não foi recuperado; resultado positivo, sozinho,
não comprova que uma aplicação pertence ao domínio histórico.

## Tratamento e proveniência

- Apenas COD medido exato, finito e não negativo é admitido, inclusive quando
  `--censor-factor` é solicitado. O modelo 1 exige data para calcular JFM.
  Pontos sem COD permanecem sem estimativas; não se inventa COD a partir de COT.
- `pfthm_modelo_1_ipt_bruto_ug_l` e `pfthm_modelo_2_ipt_bruto_ug_l` preservam
  os resultados matemáticos de cada equação. As correspondentes colunas sem
  `_bruto` aceitam apenas resultados finitos e não negativos.
- **Seleção operacional:** modelo 2 em ARPE02800 e modelo 1 nos demais pontos.
  Isso preserva a equação original nos dois pontos de origem e adota uma
  convenção explícita para os demais. Não é uma escolha estatística do melhor
  modelo, não há média e não há troca automática quando o escolhido é rejeitado.
  O mapa permite consultar cada equação separadamente, independentemente dessa seleção.
- `pfthm_ipt_bruto_ug_l` preserva o resultado matemático, inclusive negativo.
  `pfthm_predito_ipt_ug_l` aceita somente resultados finitos e não negativos.
- `modelo_pfthm_ipt` identifica a equação selecionada. `aplicacao_modelo_pfthm`
  distingue seu ponto original de extrapolação; `status_modelo_pfthm` distingue
  indisponibilidade, rejeição e estimativa histórica experimental.
- `pfthm_final_ug_l` prioriza PFTHM observado exato e só preenche sua ausência
  com estimativa utilizável. `origem_pfthm` distingue Medido, Estimado IPT COD
  e Indeterminado.
- Os campos legados `pfthm_predito_modelo_cod_ug_l` e
  `pfthm_predito_consenso_ug_l` são aliases arredondados da mesma estimativa.
  Não existe combinação de dois modelos para uma coleta.
- A reta histórica THM = max(0, (PFTHM − 21,334)/4,4) usa o PFTHM selecionado.
  Quando este é estimado, há duas estimativas encadeadas. O campo
  `thm_eta_predito_ug_l` registra essa transformação da previsão IPT.
  `thm_eta_observado_ug_l` e as probabilidades de risco continuam nulos.
- PFTHM observado >400 permanece indicador exclusivamente da medição.
  Excedências da reta histórica são estimadas, sem afirmar potabilidade.

## Avaliação temporal nos pontos originais

Sem alterar os coeficientes, foram comparados pares COD–PFTHM exatos da mesma
coleta a partir de 01/01/2013, posteriores ao ano do relatório:

| Ponto | Pares | MAE (µg/L) | RMSE (µg/L) | Viés (µg/L) | R² |
|---|---:|---:|---:|---:|---:|
| ANOR02300 | 16 | 99,94 | 155,63 | −34,51 | −0,169 |
| ARPE02800 | 54 | 287,62 | 460,51 | −226,42 | −0,835 |

Essas métricas incluem todos os resultados brutos, mesmo os negativos, para
não esconder falhas por filtragem. Em ARPE02800, 9 dos 54 resultados do teste
são negativos. Entre as 45 estimativas aceitas, o R² ainda é −0,379.
R² negativo indica erro quadrático superior ao de usar a média das observações
desse conjunto como previsão constante; não é evidência de boa generalização.
As saídas permanecem **experimentais e inadequadas para decisão regulatória**.

Nos pontos originais, há 42 estimativas aceitas em ANOR02300 e 68 em ARPE02800;
18 resultados negativos foram rejeitados. São preenchidas 14 lacunas de
PFTHM em cada ponto, totalizando 28 apenas nesses dois pontos. Não são novos
pontos de monitoramento. A avaliação das duas equações nos demais pontos,
incluindo métricas brutas, contagens e resultados negativos, está na seção
`equacoes` do relatório `avaliacao_modelos_ipt.json`; não é misturada à avaliação
nos pontos originais.

Nos outros pontos com pares exatos posteriores a 2012, a avaliação bruta foi:

| Equação transferida | Pares | Pontos | MAE (µg/L) | RMSE (µg/L) | R² | Resultados negativos |
|---|---:|---:|---:|---:|---:|---:|
| Modelo 1 | 4.753 | 112 | 146,60 | 208,40 | 0,039 | 0 |
| Modelo 2 | 4.715 | 112 | 1.029,06 | 21.435,36 | −10.415,253 | 2.647 |

Os erros incluem previsões brutas rejeitadas, especialmente extrapolações
extremas do termo quadrático. Essas métricas não sustentam tratar as equações
transferidas como previsões confiáveis. Filtrar resultados negativos não
valida automaticamente os restantes.

Na carga atual há COD exato em **13.889 coletas de 502 pontos**. O modelo 1
gera 13.889 estimativas utilizáveis; o modelo 2, 6.482 em 465 pontos, rejeitando
7.407 resultados negativos. A seleção operacional preenche 8.286 coletas sem
PFTHM medido. Somadas às 5.905 medições exatas, são 14.191 coletas com PFTHM
selecionado. Os 157 pontos sem COD exato permanecem sem previsão por essas
equações. Contagens se referem à série inteira; cada período do mapa usa sua
própria última coleta.

## Mapa e reprodução

O mapa oferece PFTHM selecionado (medido prioritário), somente medido, estimativa
selecionada, modelo 1 e modelo 2. Estimativas têm contorno tracejado. O painel identifica origem, equação,
resultado bruto, status e desempenho temporal; o gráfico separa observado e
estimado por cada modelo. Snapshots continuam selecionando uma única coleta real por período.

```powershell
python pipeline_cetesb.py --dry-run
python avaliar_modelos_ipt.py
python publicar_resultados_sql.py
python Doc/pipeline/inventariar_sql.py
python Doc/pipeline/gerar_documentacao.py
python Doc/pipeline/validar_documentacao.py
```

O relatório detalhado é `avaliacao_modelos_ipt.json`. O aplicativo também calcula
a avaliação a partir do cache carregado, evitando mostrar estatísticas antigas
depois de uma nova carga. A publicação mantém backups SQL e recria o Parquet.
