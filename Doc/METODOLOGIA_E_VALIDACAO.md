# Tratamento dos dados e limites de interpretação

Versão dos artefatos: `2026-11-16-v2`. Revisão implementada em 16/11/2026.

## Execução local e rastreabilidade

```powershell
python -m pip install -r requirements.txt
python -m unittest test_validacao -v
python reprocessar_local.py
python integrar_ana_etas.py --local-only
python app_mapa_sp.py
```

`reprocessar_local.py` não carrega `.env`, não faz requisições e não grava no
Azure. Lê os arquivos brutos da raiz, excluindo os consolidados. Produz
`resultados_v2/` com medições tratadas por ponto, tabelas wide, resumo, base THM,
predições e cache do mapa em Parquet. O manifesto inclui SHA-256, quantidade
de registros e período declarado de cada arquivo, unidades e versão.
O cache anterior e diretórios de resultados existentes são preservados em
backups identificados pela data de execução. Os arquivos XLSX originais não
são alterados. O consolidado XLSX antigo permanece uma exportação histórica
dos brutos; não representa as tabelas analíticas corrigidas.

O reprocessamento exige unicidade por ponto/data/hora/parâmetro. Períodos do
mesmo ponto são processados juntos. Duplicidades e unidades incompatíveis
interrompem a execução em vez de serem agregadas silenciosamente.
Datas inválidas interrompem a leitura; hora ausente é preservada.
Os ensaios TA98/TA100 com e sem S9 têm nomes de variáveis distintos.

O pipeline de publicação SQL continua disponível em `pipeline_cetesb.py`.
Ele utiliza o mesmo processamento por ponto de `reprocessar_local.py` e
gera também o cache local. A equivalência integral entre os caminhos de
processamento, publicação e Parquet está documentada em `EQUIVALENCIA_PIPELINE.md`.
Ele substitui tabelas (`if_exists="replace"`); antes de publicar, executar
`--dry-run` e revisar os resultados locais. O reprocessamento local não
publica automaticamente esses resultados. O mapa rejeita cache/banco com
versão anterior à revisão.

## Valores censurados e unidades

São preservados valor original, sinal, unidade original, limite numérico e
unidade padronizada. A ausência de sinal significa medição exata; `<`, `<=`,
`>` e `>=` representam intervalos, não concentrações exatas.

Por padrão, médias, índices e valores pontuais usam apenas medições exatas.
Os limites continuam disponíveis em `medicoes_tratadas/` e nos campos
`lim_*`/`qual_*` dos parâmetros principais. A triagem CONAMA utiliza os
intervalos quando permitem uma conclusão inequívoca; caso contrário, retorna
nulo. Assim, `< 0,05` não é automaticamente convertido em `0,025`, e `> 6`
não vira uma medição de `6`.

`--censor-factor` é uma opção exploratória explícita do pipeline. Não é o
padrão, não altera os brutos e não transforma resultados censurados em
medições observadas no estudo THM. Não utilizar seus índices derivados como
substitutos de resultados oficiais. O resumo informa total de resultados,
quantidade efetivamente usada na estatística, censura inferior/superior e
método utilizado. Desvio padrão é indeterminado com menos de duas observações.

Concentrações mássicas em g/L, mg/L, µg/L e ng/L são padronizadas para mg/L.
Condutividade em mS/cm é convertida para µS/cm (fator 1.000).
PFTHM, clorofila-a e feofitina-a permanecem em µg/L. Não se convertem unidades
microbiológicas ou ecotoxicológicas em concentrações mássicas.
Unidades não reconhecidas são preservadas, e incompatibilidades para um mesmo
parâmetro são rejeitadas. O manifesto registra as unidades das colunas wide;
a tabela longa retém as unidades e qualificadores de todos os parâmetros.

## Nitrogênio e variáveis derivadas

Nitrogênio total utiliza prioritariamente sua medição direta. Na ausência de
medição exata, utiliza Kjeldahl + nitrato + nitrito somente quando os três
componentes estão disponíveis. A origem fica em `origem_nitrogenio_total`.
Nitrogênio inorgânico também exige todos os componentes. Ausência não é zero.
Diferenças negativas de Kjeldahl−amoniacal e sólido total−dissolvido ficam
indeterminadas, em vez de serem artificialmente truncadas em zero.

## IET

Fonte metodológica: CETESB, Índice do Estado Trófico de Lamparelli, equações
para rios e reservatórios. PT e CL estão em µg/L; ln é o logaritmo natural.

| Ambiente | IET(PT) | IET(CL) |
|---|---|---|
| Rio/lótico | `10*(6-(0,42-0,36*ln(PT))/ln(2))-20` | `10*(6-(-0,7-0,6*ln(CL))/ln(2))-20` |
| Reservatório/lêntico | `10*(6-(1,77-0,42*ln(PT))/ln(2))` | `10*(6-(0,92-0,34*ln(CL))/ln(2))` |

O índice final é a média dos componentes disponíveis. O número de componentes
é publicado em `iet_componentes_disponiveis`; um resultado com apenas um
componente não deve ser confundido com avaliação pelos dois componentes.
Valores nulos, censurados por padrão ou não positivos não entram no logaritmo.
Ambiente desconhecido e águas identificadas como salobras/salinas não recebem
essas equações de água doce.

## IQA

A aproximação antiga está restrita a `iqa_experimental`, sem classificação
oficial e sem alimentar modelos de risco. `iqa_estimado`, sua classificação e
o indicador de IQA ótimo ficam nulos. A implementação das nove variáveis e
curvas oficiais, incluindo as regras de temperatura e microbiologia, requer
validação contra exemplos oficiais antes de ser publicada como IQA.

## CONAMA 357/2005

Os resultados são **triagem parcial por amostra**, não declaração de
conformidade legal de um corpo hídrico. `qtd_parametros_conama_avaliados`
acompanha a contagem de violações. Nulo significa indeterminado/não avaliado;
zero significa apenas que o parâmetro avaliado não excedeu aquele limite.

Fonte: Resolução CONAMA 357/2005, artigos 14–17 e 21–23.

| Água/classe | OD mínimo mg/L | DBO máximo mg/L | Turbidez máxima UNT | Faixa pH |
|---|---|---|---|---|
| Doce 1 | 6 | 3 | 40 | 6–9 |
| Doce 2 | 5 | 5 | 100 | 6–9 |
| Doce 3 | 4 | 10 | 100 | 6–9 |
| Doce 4 | maior que 2 | não avaliado | não avaliado | 6–9 |
| Salobra 1 | 5 | não avaliado | não avaliado | 6,5–8,5 |
| Salobra 2 | 4 | não avaliado | não avaliado | 6,5–8,5 |
| Salobra 3 | 3 | não avaliado | não avaliado | 5–9 |

Fósforo total (mg/L P): águas doces classes 1/2/3 usam, respectivamente,
0,02/0,03/0,05 em ambiente lêntico; 0,025/0,05/0,075 em intermediário;
0,1/0,1/0,15 em lótico. Para salobras 1/2: 0,124/0,186.
O cadastro local não descreve todos os tempos de residência e tributários;
essa condição impede tratar a triagem como uma conclusão regulatória integral.

Amônia em água doce depende de pH medido: faixas ≤7,5; ≤8; ≤8,5; >8,5.
Classes 1/2: 3,7; 2; 1; 0,5 mg/L N. Classe 3: 13,3; 5,6; 2,2; 1.
Salobras 1/2: 0,4/0,7. Ausência de pH impede a avaliação dependente dele.
Classe especial preserva condições naturais e não recebe limites de classe 1
ou 2. Classes desconhecidas não são preenchidas automaticamente.
E. coli não recebe automaticamente o limite de coliformes termotolerantes:
faltam regras de substituição, uso e avaliação temporal.

## PFTHM, THM e modelos IPT

Documentos históricos de referência em `Doc`:

- Relatório Técnico 125 967-205 (2012): figura na página 9 do PDF,
  coeficientes na página 12, modelos por manancial nas páginas 13–14.
- Apresentação da fase exploratória: slide 40, coeficientes espaciais.
- MapasTHM: mapas históricos; não constituem base numérica de validação.

Desde `2026-11-17-v4-ipt-cod`, as duas equações COD das linhas 1 e 2 da tabela
1 (seção 3, PDF p.13) são calculadas separadamente em todos os pontos com COD
exato. Fora de seus pontos originais, ANOR02300 e ARPE02800 respectivamente,
são extrapolações experimentais não validadas. Resultados negativos são rejeitados,
sem conversão a zero, e os valores brutos permanecem rastreáveis.
PFTHM medido tem prioridade. Na ausência, usa-se o modelo 2 em ARPE02800 e
o modelo 1 nos demais pontos, como convenção operacional, sem média ou troca
após rejeição. Os dois modelos permanecem disponíveis separadamente no mapa.
Equações, proveniência e avaliação estão em `pipeline/MODELOS_IPT_COD.md`.
`thm_eta_observado_ug_l` e os campos de risco **observado** ficam nulos, pois
não existem medições correspondentes de THM na saída da ETA nos arquivos.

A inversa algébrica da figura histórica é:

`THM_estimado = max(0, (PFTHM - 21,334) / 4,4)`

Ela é identificada como `thm_eta_estimado_reta_historica_ug_l`. A coluna legada
`thm_eta_final_ug_l` contém a mesma estimativa para compatibilidade do mapa.
Não se trata de uma nova regressão, medição na ETA, comprovação de vínculo
hídrico ou demonstração de potabilidade. Os limiares matemáticos correspondem
a PFTHM de 461,334 para THM 100 e 373,334 para THM 80. O indicador histórico
PFTHM > 400 fica separado como `excede_pfthm_400_proxy_ipt_bin`.

**Probabilidades e classificações dos modelos não validados ficam nulas.**
Não há imputação automática de chuva, DBO, OD, fósforo, pH, temperatura ou COT
para gerar riscos. Não se estima UV254 por `0,028*COD`.
Os status distinguem ausência, estimativa, extrapolação e rejeição; os modelos
logísticos de risco permanecem indisponíveis.

Para reativação, são necessários:

1. Modelo físico-químico: UV254 medido e coeficientes com precisão suficiente.
   DBO² aparece como 0,00 na tabela, apesar de Z = −5,60; não se deve inferir
   um coeficiente exato a partir desse arredondamento.
2. Modelo espacial: IQA compatível com o estudo, domínio dos 65 pontos e seus
   coeficientes específicos. Não existe respaldo para substituir os efeitos
   por um único `1,520 * (COT > 5)`.
3. Modelos PFTHM: as equações COD estão ativas como experimentais. O teste
   posterior a 2012 nos pontos originais apresentou R² negativo para ambas.
   Uso como predição validada requer revisão do domínio/calibração e teste
   independente; transferência para outros pontos não é validada pelo ajuste original.
4. Risco na ETA: pares de observações de água bruta e tratada com ligação de
   abastecimento confirmada. A aderência histórica não valida a aplicação
   atual aos demais pontos.

## Extração, ANA e visualização

O scraper verifica ponto, datas e estrutura dos XLSX recebidos. Só reutiliza
arquivo com manifesto compatível com as datas completas e SHA-256 íntegro.
Arquivos antigos sem manifesto são rebaixados na próxima extração.
`--overwrite` preserva o arquivo anterior até o download substituto estar
validado; a gravação final é atômica. As janelas sem dados são registradas.
Somente mensagens explícitas reconhecidas de ausência de dados recebem esse
status; erros HTTP ou mensagens ambíguas interrompem/tentam novamente.
Uma nova mensagem legítima do portal deve ser adicionada à lista após
verificação, sem generalizar por palavras soltas.

Na ANA, validação TLS permanece habilitada. Erros HTTP, objetos de erro,
respostas malformadas e paginação inválida não são aceitos como sucesso.
`integrar_ana_etas.py` só publica no banco com `--publish-db`.
Coordenadas de captação não substituem coordenadas desconhecidas da planta.

As relações por distância e semelhança de nome são **candidatas**, nunca
conexões comprovadas. Não são produzidas candidatas além de 15 km.
`vinculo_confirmado` é falso. Confirmar conexão exige hidrografia, posição na
drenagem e dados do sistema de abastecimento.

Os snapshots mensais/trimestrais/anuais selecionam a última linha real,
ordenando por data e hora, sem buscar valores não nulos em coletas anteriores.
O HTML ilustrativo `etas.html` tem sintaxe corrigida e não declara auditoria
geográfica sem fontes.

## Verificação automatizada

`test_validacao.py` cobre medição/soma de nitrogênio, censura, conversão de
unidades, ensaios S9 distintos, IET lêntico, classes CONAMA, ausência de pH,
limites de intervalos, ausência de riscos inventados, snapshots, preservação
de downloads, manifestos, erros/paginação ANA e esquemas de consolidação.
Inclui regressões com BILL02030 e ABAN02790 quando esses arquivos estão
presentes. Os testes não acessam o portal, a API ANA nem o banco.

Esses testes verificam a implementação e casos conhecidos; não substituem
validação científica dos modelos nem conferência de completude na fonte.
