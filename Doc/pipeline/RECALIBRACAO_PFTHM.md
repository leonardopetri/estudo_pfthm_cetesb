# Recalibração de PFTHM em tabela separada

Execução: `pfthm_recal_20261119T221745Z`.
Tabela publicada: **`dbo.predicoes_pfthm_recalibradas`**.
São 39.750 coletas, 23 colunas, com 13.888 previsões selecionadas disponíveis
em 502 pontos. A tabela histórica `predicoes_risco_thm` preserva as equações
históricas. Posteriormente, a pedido do usuário, o modelo selecionado foi
integrado ao mapa conforme a seção abaixo.

Artefatos completos em
`resultados_recalibracao/pfthm_recal_20261119T221745Z/`:

- `predicoes_pfthm_recalibradas.parquet`: conteúdo publicado;
- `modelos_e_avaliacao.json`: coeficientes completos, efeitos por ponto,
  partições, seleção, métricas, versões das dependências e hashes;
- `publicacao_sql.json`: verificação integral antes e depois da publicação.

## Dados e separação entre ajuste e teste

Usaram-se 5.597 pares exatos COD–PFTHM da mesma coleta em 113 pontos.
O alvo é exclusivamente `pfthm_observado_ug_l`; previsões históricas e
`pfthm_final_ug_l` não são usados como medições ou variáveis explicativas.
COD está em mg/L e PFTHM em µg/L.

O protocolo foi fixado antes do ajuste:

| Etapa | Coletas | Pontos | Uso |
|---|---:|---:|---|
| Treino inicial | 2.214 | 64 | Anos até 2020 nos pontos de desenvolvimento |
| Validação temporal | 451 | 61 | 2021–2022, sem participar do ajuste inicial |
| Validação espacial | 657 | 17 | Até 2022, em pontos separados do treino inicial |
| Ajuste final | 3.322 | 82 | Reunião de treino e validações, até 2022 |
| Teste temporal | 1.105 | 81 | 2023–2026; excluído de ajuste e seleção |
| Teste espacial | 1.170 | 24 | Pontos inteiros, em todos os anos; excluído de ajuste e seleção |

Os pontos são particionados deterministicamente por SHA-256 do código com
prefixo `pfthm-recal-v1:` e módulo 5: grupo 0 para teste espacial, grupo 1
para validação espacial e grupos 2–4 para desenvolvimento. A associação não
usa valores de COD ou PFTHM. Os testes temporal e espacial não se sobrepõem.
Pontos novos surgidos depois de 2022 são identificados também no teste temporal.

A escolha minimizou a média dos RMSE **brutos** das duas validações, dando
o mesmo peso à avaliação temporal e à espacial. Só depois da escolha foram
reajustados os coeficientes usando treino + validações. Os testes permaneceram
fora do ajuste final. Não foi realizado um ajuste posterior com todos os dados.

## Modelos comparados e coeficientes finais

1. Linear: `PFTHM = a + b × COD + c × JFM`.
2. Quadrático: `PFTHM = a + b × COD + c × COD²`.
3. Conjunto com ajuste por ponto: estrutura linear/JFM mais intercepto por
   estação, com penalização quadrática dos interceptos locais. Foram comparados
   λ = 1, 10, 100 e 1.000. Os coeficientes globais não são penalizados.
   Pontos desconhecidos recebem ajuste local zero. O melhor λ foi 10.

As equações históricas do IPT e a média do conjunto de ajuste foram usadas
como referências. A média não concorreu à escolha de estrutura recalibrada.

O **modelo quadrático recalibrado** foi selecionado na validação:
critério RMSE médio de 164,29 µg/L, versus 166,29 do melhor modelo com ajustes
locais e 173,92 do linear. Isso é uma comparação agregada, não prova de
superioridade em cada estação.

Coeficientes apresentados com arredondamento; valores completos constam no JSON:

```text
Modelo 1:
PFTHM = 240,128270 + 15,064517 × COD + 109,384090 × JFM

Modelo 2 — selecionado:
PFTHM = 152,945813 + 45,810177 × COD − 0,280725389 × COD²

Modelo com ajustes por ponto, λ = 10:
PFTHM = 250,887901 + 12,156103 × COD + 111,142564 × JFM + efeito_do_ponto
```

JFM vale 1 em janeiro–março e 0 nos demais meses. O ajuste usa COD centralizado
e escalado para estabilidade numérica; o JSON fornece também os coeficientes
convertidos para COD nas unidades originais. Nenhum extremo foi excluído
porque piorava o ajuste; os dados incluem concentrações de COD elevadas.

## Desempenho nos testes reservados

Métricas brutas, antes de rejeições de domínio, em µg/L:

| Teste | Modelo | MAE | RMSE | R² |
|---|---|---:|---:|---:|
| Temporal, 1.105 coletas | Média do ajuste | 150,70 | 225,01 | −0,019 |
| Temporal | IPT histórico 1 | 153,75 | 218,85 | 0,036 |
| Temporal | IPT histórico 2 | 474,93 | 675,31 | −8,183 |
| Temporal | Linear recalibrado | 135,06 | 204,81 | 0,155 |
| Temporal | Modelo com ajustes locais | 122,08 | 194,02 | 0,242 |
| Temporal | **Quadrático recalibrado** | **120,69** | **188,09** | **0,288** |
| Espacial, 1.170 coletas | Média do ajuste | 168,22 | 244,43 | −0,017 |
| Espacial | IPT histórico 1 | 161,69 | 235,86 | 0,053 |
| Espacial | IPT histórico 2 | 1.578,27 | 31.767,87 | −17.184,853 |
| Espacial | Linear recalibrado | 148,32 | 229,23 | 0,105 |
| Espacial | Modelo com ajustes locais | 150,40 | 228,18 | 0,113 |
| Espacial | **Quadrático recalibrado** | **131,84** | **204,25** | **0,290** |

O selecionado subestima em média 22,03 µg/L no teste temporal e 23,21 µg/L
no espacial. A melhora é parcial: erros permanecem relevantes e aproximadamente
71% da variabilidade dos testes não é explicada pela equação. Não se afirmou
significância estatística da diferença entre candidatos.

O teste espacial contém uma observação acima da faixa de COD usada no ajuste.
Ela está incluída nas métricas brutas. Na previsão selecionada utilizável ela
fica nula; nas outras 1.169 observações, o RMSE espacial é 204,22 µg/L.
O JSON separa avaliações brutas, dentro do domínio, por ponto conhecido/novo
e RMSE médio por ponto.

## Campos da tabela

| Campos | Significado |
|---|---|
| `codigo_ponto`, `data_coleta`, `hora_coleta` | Chave da coleta para comparação com a base histórica |
| `cod_observado_mg_l`, `pfthm_observado_ug_l` | Medições exatas copiadas da base; não são preenchidas por modelos |
| `pfthm_recal_modelo1_bruto_ug_l` | Previsão do linear recalibrado |
| `pfthm_recal_modelo2_bruto_ug_l` | Previsão do quadrático recalibrado |
| `pfthm_recal_misto_bruto_ug_l` | Previsão da alternativa com efeitos por ponto e λ selecionado |
| `pfthm_recalibrado_bruto_ug_l` | Previsão bruta do modelo selecionado |
| **`pfthm_recalibrado_ug_l`** | **Previsão selecionada finita, não negativa e dentro da faixa COD de ajuste** |
| `modelo_selecionado`, `modelo_misto_comparado` | Identificadores da estrutura selecionada e da alternativa local |
| `status_predicao` | Insumo indisponível, resultado inválido, fora do domínio ou previsão experimental |
| `cod_min_ajuste_mg_l`, `cod_max_ajuste_mg_l` | Faixa observada no ajuste final: 1–159 mg/L |
| `fora_faixa_cod_ajuste_bin` | 1 fora da faixa, 0 dentro, nulo quando insumo indisponível |
| `grupo_particao_ponto` | Partição determinística por estação |
| `etapa_selecao` | Papel da coleta na seleção inicial |
| `papel_avaliacao_final` | Ajuste final, teste temporal, teste espacial ou sem par medido elegível |
| `usado_ajuste_final_bin` | 1 somente nas 3.322 observações usadas nos coeficientes finais |
| `ponto_no_ajuste_bin` | Indica se houve alguma observação dessa estação no ajuste final |
| `versao_modelo`, `versao_dados_origem` | Execução de recalibração e versão da base de observações |

As previsões são mantidas mesmo quando há PFTHM medido, para permitir
comparação. O campo selecionado **não substitui** a previsão por sua observação.
As linhas de ajuste possuem previsões internas à amostra; não devem ser
contadas como teste. A métrica de validação do JSON vem do ajuste inicial;
as previsões da tabela vêm do reajuste final, que já inclui as validações.

Há 25.861 coletas sem COD exato/data utilizável, uma fora da faixa de COD,
5.763 previsões em estações com dados no ajuste e 8.125 em outras estações.
Ausência de PFTHM medido não impede previsão quando COD está disponível.

A faixa mínimo–máximo é uma proteção de extrapolação, não uma garantia de
adequação ambiental ou de boa precisão. Não há aprovação regulatória,
probabilidade de risco de THM ou recalibração da conversão histórica para THM na ETA.

## Reprodução e publicação

```powershell
python -m unittest test_validacao.RecalibrationTests -v
python recalibrar_pfthm.py
python recalibrar_pfthm.py --publicar-sql
```

Cada execução cria um diretório novo. A publicação verifica todos os valores
em uma tabela temporária antes da troca transacional. Se a tabela de
recalibração já existir, ela é preservada em schema de backup, com permissões
do objeto reaplicadas à nova versão. Nenhuma tabela histórica é substituída.

Nesta execução passaram 28 testes da suíte, incluindo bloqueio de vazamento:
alterar os alvos dos testes reservados não altera coeficientes nem seleção.
A publicação conferiu integralmente 39.750 linhas × 23 colunas antes e depois
da troca, com diferença numérica máxima zero. O hash da base de estudo e o
hash do cache histórico foram conferidos e permaneceram iguais.

O dicionário geral de `Doc/pipeline` é um retrato da publicação histórica
anterior. Esta tabela adicional é documentada aqui e nos artefatos desta execução.

## Integração ao mapa

O aplicativo deste repositório é `app_mapa_sp.py`. A camada padrão
**PFTHM medido ou recalibrado** usa a medição exata e, na ausência dela,
o quadrático recalibrado da mesma coleta. A opção **Quadrático recalibrado**
exibe somente a previsão, inclusive quando existe uma medição para comparação.
As equações históricas permanecem em opções identificadas como comparação.
Sem COD ou fora de 1–159 mg/L não há preenchimento pela equação antiga.

`modelo_pfthm_mapa.py` contém os coeficientes congelados, a normalização e a
lista de pontos que contribuíram para o ajuste. Ele não retreina modelos.
A mesma função é usada pelo cache do pipeline, pelo reprocessamento local,
pela consulta SQL do aplicativo e pela migração de caches antigos.
Isso também permite aplicar a equação a coletas novas sem depender de uma
linha já existente na tabela de avaliação `predicoes_pfthm_recalibradas`.
A tabela separada continua sendo o registro da execução de calibração.

Campos acrescentados ao cache:

- `pfthm_recalibrado_ug_l` e `pfthm_recalibrado_bruto_ug_l`: previsão aceita e bruta;
- `pfthm_mapa_ug_l` e `origem_pfthm_mapa`: seleção medida/recalibrada da mesma coleta;
- `status_modelo_recalibrado`, `ponto_no_ajuste_recalibrado_bin` e
  `fora_faixa_cod_recalibrado_bin`: disponibilidade, proveniência e domínio;
- `thm_eta_mapa_estimado_ug_l`: reta histórica aplicada ao PFTHM selecionado;
- `versao_modelo_recalibrado` e `versao_cache_mapa`: versão do modelo e do cache.

Os nomes históricos `pfthm_final_ug_l` e `origem_pfthm` continuam descrevendo a
seleção antiga, para auditoria. Não devem ser confundidos com os novos campos
`*_mapa`. A conversão PFTHM–THM não foi recalibrada. Nenhuma probabilidade de
risco foi habilitada.

Os snapshots mantêm uma única coleta por ponto/período. Na API compacta,
as posições antigas 0–25 foram preservadas; acrescentaram-se 26=previsão
recalibrada, 27=PFTHM do mapa, 28=origem do mapa, 29=THM estimado do mapa,
30=status da recalibração e 31=versão do modelo. O endpoint de estação fornece
os campos por nome. O painel e o gráfico distinguem medição, recalibração
e equações históricas.

A integração foi comparada integralmente com o SQL: o cache possui 39.750
linhas e 129 colunas, com igualdade exata entre os caminhos local e SQL;
os resultados recalibrados também coincidem com a tabela separada.
A camada padrão tem 5.905 medições, 8.292 estimativas e 25.553 valores
indeterminados, totalizando 14.197 coletas com PFTHM. São seis preenchimentos
a mais que na seleção histórica, por previsões antes rejeitadas em ARPE02800.

Os 502 pontos com previsão referem-se à série completa. No trimestre parcial
2026-T4, a última coleta de cada ponto contém COD utilizável em apenas três
pontos. O mapa não preenche a coleta atual com COD ou PFTHM de datas anteriores.
O fundo usa OpenStreetMap, com atribuição, após o serviço CARTO retornar imagens
com exigência de chave durante a validação visual.
