# Aquisição, tratamento e recalibração de dados de qualidade de águas superficiais (CETESB/InfoÁguas, 2010–2026)

Fontes: `METODOLOGIA_E_VALIDACAO.md`, `RESULTADOS_VALIDACAO.md`,
`pipeline/MODELOS_IPT_COD.md`, `pipeline/RECALIBRACAO_PFTHM.md`.
Versões dos artefatos: base `2026-11-17-v4-ipt-cod`; recalibração `pfthm_recal_20261119T221745Z`.

## 1. Aquisição dos dados

A base primária foi obtida do sistema InfoÁguas (CETESB) por extração automatizada
(`scraper_infoaguas.py`, Playwright). O procedimento consistiu em: (i) autenticação;
(ii) leitura do catálogo de pontos de monitoramento; (iii) seleção por UGRHI ou código
de ponto; (iv) requisição do relatório de Qualidade de Águas Superficiais por ponto,
em janelas de até cinco anos (restrição do servidor), cobrindo 2010–2026, com
persistência de um arquivo XLSX por ponto.

Cada arquivo é validado quanto à correspondência de ponto, intervalo de datas e
estrutura tabular, e registrado em manifesto com SHA-256, contagem de registros e
período declarado. Apenas mensagens explícitas e reconhecidas de ausência de dados
são classificadas como janelas vazias; erros HTTP ou respostas ambíguas resultam em
nova tentativa ou interrupção. A gravação é atômica, preservando o arquivo anterior
até a validação do substituto.

Resultado: **659 arquivos / 659 pontos**, **1.700.130 medições** e **39.750 coletas**
(chave: ponto, data, hora).

Complementarmente, dados de captações e estações de tratamento de água (ETAs) foram
obtidos da ANA (`integrar_ana_etas.py`), com validação TLS ativa e rejeição de
respostas malformadas ou paginação inválida: 503 captações e 544 relações
CETESB–ETA candidatas, geradas por distância (≤ 15 km) e similaridade nominal.
Essas relações são classificadas como hipóteses (`vinculo_confirmado = falso`);
a confirmação exigiria hidrografia, posição na rede de drenagem e dados do sistema
de abastecimento.

## 2. Transformações de dados

O processamento (`pipeline_cetesb.py`; execução local equivalente em
`reprocessar_local.py`) converte os brutos em: tabela longa de medições por ponto
(Parquet particionado), tabela *wide* por coleta (39.750 × 568), resumo por
parâmetro, base de estudo THM, tabela de predições e cache geoespacial.
A equivalência entre os caminhos de processamento está documentada em
`EQUIVALENCIA_PIPELINE.md`.

### 2.1 Integridade estrutural

- Unicidade obrigatória por ponto/data/hora/parâmetro. Duplicidades, datas inválidas
  e unidades incompatíveis para o mesmo parâmetro interrompem a execução; não há
  agregação implícita. Hora ausente é preservada como nula.
- Ensaios de mutagenicidade TA98/TA100 com e sem ativação metabólica (S9), que
  colidiam após normalização de nomes, foram separados em variáveis distintas.
- Manifesto por execução com hashes das fontes e dos scripts, totais e versão.
  Os XLSX originais não são modificados.

### 2.2 Dados censurados

Para cada medição são preservados valor original, qualificador (`=`, `<`, `<=`,
`>`, `>=`), unidade original, limite numérico e unidade padronizada. Por padrão,
estatísticas descritivas, índices e modelos utilizam **apenas medições exatas**
(`=`); limites de quantificação não são substituídos por frações do limite
(e.g., `< 0,05` não é imputado como 0,025). A substituição por fator de censura
é opcional, explícita (`--censor-factor`) e não é aplicada ao estudo THM. O resumo
por parâmetro reporta total de resultados, quantidade utilizada, censura inferior e
superior e método empregado; desvio padrão é indeterminado com n < 2.

### 2.3 Padronização de unidades

Concentrações mássicas (ng/L, µg/L, mg/L, g/L) são convertidas para mg/L, exceto
PFTHM, clorofila-a e feofitina-a, mantidos em µg/L. Condutividade em mS/cm é
convertida para µS/cm (×1.000). Unidades microbiológicas e ecotoxicológicas não
são convertidas. Unidades não reconhecidas são preservadas.

### 2.4 Variáveis derivadas

- Nitrogênio total: prioridade à medição direta; na ausência, soma
  Kjeldahl + nitrato + nitrito somente quando os três componentes existem como
  medições exatas (ausência ≠ zero). Origem registrada em `origem_nitrogenio_total`.
  Resultado: 4.433 diretas, 13.519 somas completas, 21.798 indeterminadas.
- Nitrogênio inorgânico: mesma exigência de completude.
- Diferenças Kjeldahl − amoniacal e sólidos totais − dissolvidos negativas são
  marcadas como indeterminadas, não truncadas em zero.

### 2.5 Índices e triagem normativa

- **IET** (Lamparelli/CETESB), com equações distintas para ambientes lótico e
  lêntico, componentes PT e CL em µg/L, média dos componentes disponíveis e
  publicação de `iet_componentes_disponiveis`. Valores censurados ou não positivos
  são excluídos do logaritmo; ambientes desconhecidos ou salobros/salinos não
  recebem as equações de água doce.
- **IQA**: a aproximação anterior foi restrita a `iqa_experimental`, sem
  classificação oficial e sem uso em modelos, por não implementar integralmente as
  nove curvas oficiais.
- **CONAMA 357/2005**: triagem por amostra de OD, DBO, turbidez, pH, fósforo total
  (por classe e regime hidráulico) e amônia (dependente de pH), com contagem de
  parâmetros avaliados. Nulo denota indeterminação; a saída é triagem parcial,
  não declaração de conformidade do corpo hídrico. Classe especial e classes
  desconhecidas não recebem limites por inferência.

### 2.6 Modelos históricos de PFTHM (IPT, RT 125967-205, 2012)

As equações da Tabela 1 do relatório foram implementadas e avaliadas:

| Ponto de origem | Equação (µg/L) | R² original |
|---|---|---:|
| ANOR02300 | `235 + 15,9·COD + 282·JFM` | 81,5% |
| ARPE02800 | `−1983 + 669·COD − 43,3·COD²` | 62,0% |

COD em mg/L; JFM = 1 em janeiro–março. Ambas são calculadas em todos os pontos com
COD exato (13.889 coletas, 502 pontos), com aplicação fora do ponto de origem
rotulada como extrapolação experimental. Resultados negativos são rejeitados e
preservados em colunas `_bruto`. Seleção operacional: modelo 2 em ARPE02800,
modelo 1 nos demais; PFTHM medido tem prioridade (`pfthm_final_ug_l`).

Avaliação temporal com pares exatos pós-2012: nos pontos originais, R² = −0,169
(ANOR02300, n = 16) e −0,835 (ARPE02800, n = 54; 9 previsões negativas).
Transferidas aos demais pontos: modelo 1, R² = 0,039 (n = 4.753); modelo 2,
colapso numérico (2.647 previsões negativas, RMSE > 21.000 µg/L).

Os modelos logísticos de risco de THM do estudo original foram **desativados**
(probabilidades nulas), por ausência de UV254 medido, de IQA compatível e de
pares água bruta/tratada. Não há imputação de insumos; foram eliminados 1.622
casos em que probabilidade ausente havia sido codificada como risco zero.
A conversão histórica `THM = max(0, (PFTHM − 21,334)/4,4)` é mantida apenas como
estimativa identificada, sem valor de medição na ETA.

### 2.7 Publicação e verificação

Seis tabelas derivadas foram publicadas em Azure SQL (schema `dbo`) por carga em
tabela temporária, conferência integral e troca transacional, com versões
anteriores preservadas em schema de backup. A suíte `test_validacao.py` cobre
censura, unidades, nitrogênio, IET, CONAMA, snapshots temporais, manifestos,
tratamento de erros ANA e ausência de riscos não validados, incluindo regressões
com BILL02030 e ABAN02790.

## 3. Recalibração do modelo de PFTHM

Dado o desempenho inadequado das equações históricas, realizou-se recalibração
(`recalibrar_pfthm.py`), publicada em tabela independente
(`dbo.predicoes_pfthm_recalibradas`), sem alteração das tabelas históricas.

### 3.1 Dados

5.597 pares exatos COD (mg/L) – PFTHM (µg/L) da mesma coleta, em 113 pontos.
Variável resposta: exclusivamente `pfthm_observado_ug_l`. Previsões históricas e
valores preenchidos não participam como resposta nem como preditores.

### 3.2 Protocolo de partição

Fixado antes de qualquer ajuste. Os pontos foram particionados deterministicamente
por SHA-256 do código (prefixo `pfthm-recal-v1:`, módulo 5), sem uso de valores de
COD ou PFTHM: grupo 0 → teste espacial; grupo 1 → validação espacial; grupos 2–4 →
desenvolvimento. No desenvolvimento, aplicou-se corte temporal.

| Etapa | Coletas | Pontos | Critério |
|---|---:|---:|---|
| Treino inicial | 2.214 | 64 | Desenvolvimento, anos ≤ 2020 |
| Validação temporal | 451 | 61 | Desenvolvimento, 2021–2022 |
| Validação espacial | 657 | 17 | Grupo 1, anos ≤ 2022 |
| Ajuste final | 3.322 | 82 | Treino + validações, ≤ 2022 |
| Teste temporal | 1.105 | 81 | 2023–2026, excluído de ajuste e seleção |
| Teste espacial | 1.170 | 24 | Grupo 0, todos os anos, excluído de ajuste e seleção |

Os conjuntos de teste são disjuntos entre si e não participaram de ajuste nem de
seleção. Não foi realizado reajuste com a totalidade dos dados.

### 3.3 Estruturas candidatas

1. Linear com sazonalidade: `PFTHM = a + b·COD + c·JFM`.
2. Quadrático: `PFTHM = a + b·COD + c·COD²`.
3. Misto: estrutura (1) com intercepto por ponto sob penalização quadrática
   (ridge) dos efeitos locais, λ ∈ {1, 10, 100, 1.000}; coeficientes globais não
   penalizados; pontos não vistos recebem efeito local zero. Melhor λ = 10.

Referências não concorrentes: média do conjunto de ajuste e as duas equações IPT.
COD foi centralizado e escalado para condicionamento numérico; os coeficientes
são reportados também na escala original. Nenhuma observação foi excluída.

### 3.4 Seleção e ajuste final

Critério de seleção: média aritmética dos RMSE brutos das validações temporal e
espacial. Resultados: quadrático 164,29 µg/L; misto (λ = 10) 166,29; linear 173,92.
Selecionado o quadrático, os coeficientes foram reajustados em treino + validações
(3.322 observações):

```
PFTHM = 152,945813 + 45,810177·COD − 0,280725389·COD²
```

Faixa de COD no ajuste final: 1–159 mg/L.

### 3.5 Desempenho nos conjuntos de teste

Métricas brutas (sem filtragem de domínio), em µg/L:

| Teste | Modelo | MAE | RMSE | R² |
|---|---|---:|---:|---:|
| Temporal (n = 1.105) | Média do ajuste | 150,70 | 225,01 | −0,019 |
| Temporal | IPT 1 | 153,75 | 218,85 | 0,036 |
| Temporal | IPT 2 | 474,93 | 675,31 | −8,183 |
| Temporal | Linear recalibrado | 135,06 | 204,81 | 0,155 |
| Temporal | Misto (λ = 10) | 122,08 | 194,02 | 0,242 |
| Temporal | **Quadrático recalibrado** | **120,69** | **188,09** | **0,288** |
| Espacial (n = 1.170) | Média do ajuste | 168,22 | 244,43 | −0,017 |
| Espacial | IPT 1 | 161,69 | 235,86 | 0,053 |
| Espacial | IPT 2 | 1.578,27 | 31.767,87 | −17.184,853 |
| Espacial | Linear recalibrado | 148,32 | 229,23 | 0,105 |
| Espacial | Misto (λ = 10) | 150,40 | 228,18 | 0,113 |
| Espacial | **Quadrático recalibrado** | **131,84** | **204,25** | **0,290** |

O modelo selecionado apresenta viés médio de −22,03 µg/L (temporal) e −23,21 µg/L
(espacial), explicando aproximadamente 29% da variância nos testes. O ganho em
relação às equações históricas é consistente em ambos os eixos de generalização,
mas o erro residual permanece elevado; não foi testada a significância das
diferenças entre candidatos. Uma observação do teste espacial excede a faixa de COD
do ajuste; sem ela, o RMSE espacial é 204,22 µg/L.

### 3.6 Publicação e controles

A tabela contém 39.750 coletas × 23 colunas, com 13.888 previsões utilizáveis em
502 pontos. A previsão utilizável (`pfthm_recalibrado_ug_l`) exige resultado
finito, não negativo e COD dentro da faixa de ajuste; `status_predicao` discrimina
insumo indisponível, resultado inválido, fora do domínio e previsão experimental.
Cada linha registra a partição do ponto, o papel na avaliação (ajuste final, teste
temporal, teste espacial, sem par elegível) e `usado_ajuste_final_bin`. Previsões
são mantidas mesmo onde há PFTHM medido, para comparação, e não substituem a
observação.

Controles: testes de bloqueio de vazamento (perturbação dos alvos dos conjuntos de
teste não altera coeficientes nem seleção); publicação via tabela temporária com
conferência integral de 39.750 × 23 valores antes e depois da troca (diferença
máxima zero); hashes da base de estudo e do cache histórico verificados. Artefatos
completos (coeficientes, efeitos por ponto, partições, métricas, versões e hashes)
em `resultados_recalibracao/pfthm_recal_20261119T221745Z/`.

A recalibração não produz probabilidade de risco de THM, não recalibra a conversão
PFTHM → THM na ETA e não constitui validação regulatória.

## 4. Discussão: implicações e limitações

Estatísticas calculadas sobre `resultados_v2/estudo_thm_base.parquet`
(PFTHM observado exato) e sobre a tabela de recalibração publicada.

### 4.1 Distribuição do PFTHM observado (2010–2026)

5.905 medições exatas em 120 pontos.

| Estatística | µg/L |
|---|---:|
| Mediana | 294 |
| P75 / P90 / P95 / P99 | 428 / 588 / 703 / 1.060 |
| Máximo | 2.650 |

| Limiar | Fração das amostras |
|---|---:|
| > 100 µg/L | 95,9% |
| > 373 µg/L (THM 80 pela reta histórica) | 33,4% |
| > 400 µg/L (proxy histórico IPT) | 28,8% |
| > 461 µg/L (THM 100 pela reta histórica) | 20,9% |

Entre os 98 pontos com n ≥ 20, 14 apresentam mediana superior a 400 µg/L.
Os maiores valores medianos ocorrem em RGDE02030 (637), AVEC02800 (570),
PCAB02220 (548), TIET02090 (505), SORO02300 (479), BMIR02800 (477), COGR00900
(438), JNDI00500 (429), CASC02050 (422) e GUAR00900 (418), com 58–81% das
amostras acima de 400 µg/L. Diversos desses códigos correspondem a reservatórios
e rios utilizados para abastecimento público na Região Metropolitana de São Paulo
e no interior; o vínculo manancial–ETA, contudo, não foi confirmado na base
(seção 1).

A mediana anual elevou-se de 215–253 µg/L (2010–2014) para 308–383 µg/L
(2018–2026, exceto 2024, com 280 µg/L). A tendência não foi submetida a teste estatístico formal
(e.g., Mann-Kendall sazonal por ponto) e deve ser tratada como hipótese.

As 13.888 previsões recalibradas utilizáveis reproduzem o mesmo quadro:
24,7% excedem 400 µg/L (mediana 332; P75 398 µg/L), estendendo a leitura a 502
pontos sem PFTHM medido, com a incerteza individual descrita na seção 3.5.

### 4.2 Conclusões sustentadas pelos dados

1. **Pressão elevada e disseminada de precursores de THM na água bruta.**
   Aproximadamente um terço das amostras excede o proxy histórico de 400 µg/L e,
   em 14 mananciais, essa condição é a mediana. Configura-se um fator de risco a
   montante para a formação de trialometanos na desinfecção por cloro, com
   indicação de agravamento ao longo da série.

2. **COD isolado é preditor insuficiente de PFTHM.** As equações históricas não
   generalizaram (R² ≤ 0,05 fora do ajuste; negativo nos pontos de origem). O
   modelo recalibrado reduziu o erro, mas explica cerca de 29% da variância nos
   testes, com RMSE (188–204 µg/L) da mesma ordem da distância entre a mediana
   observada e o limiar de 400 µg/L. A superioridade do termo quadrático e a
   falha da estrutura linear com sazonalidade indicam saturação da relação em
   concentrações elevadas e sugerem que a composição da matéria orgânica
   (aromaticidade/SUVA, origem algal ou terrestre, brometo) tem peso maior que a
   concentração total. O uso adequado do modelo é a triagem e priorização de
   monitoramento, não a classificação de coletas individuais.

### 4.3 Conclusões não sustentadas pelos dados

A base **não permite afirmar risco à saúde do consumidor**, por três razões:

- PFTHM é um ensaio de potencial (cloração em excesso, tempo de contato
  prolongado, condições padronizadas): representa o limite superior de
  precursores, não a concentração de THM na água distribuída. O THM efetivo
  depende da remoção de precursores na coagulação, da pré-oxidação, do tipo e
  dose de desinfetante (cloraminação reduz substancialmente a formação), de pH,
  temperatura e tempo de residência na rede.
- Não há medição de THM em saída de ETA ou rede de distribuição
  (`thm_eta_observado_ug_l` nulo em todas as linhas). A conversão
  `THM = (PFTHM − 21,334)/4,4` é uma relação de 2012, derivada de um único
  sistema, não recalibrada e sem avaliação posterior.

Formulação defensável: os mananciais analisados apresentam potencial de formação
de THM frequentemente compatível com excedência do padrão de potabilidade para
THM total (100 µg/L, Portaria GM/MS nº 888/2021) **caso o tratamento não remova
precursores de forma adequada**. Não se conclui que a água distribuída exceda o
padrão nem que haja exposição da população.

### 4.4 Requisitos para avaliação de risco ao consumidor

1. Séries de THM em água tratada por sistema de abastecimento (SISAGUA/Ministério
   da Saúde; relatórios de qualidade das concessionárias), pareadas temporalmente
   com o PFTHM do manancial correspondente.
2. Confirmação do vínculo manancial–ETA por cadastro de outorga e dados
   operacionais, em substituição ao critério de distância.
3. Inclusão de UV254/SUVA, brometo, pH, temperatura e dose de cloro como
   preditores, para modelar THM efetivo em vez de potencial.
4. Teste formal de tendência temporal do PFTHM por manancial e análise de
   sazonalidade associada a eventos de floração algal.
