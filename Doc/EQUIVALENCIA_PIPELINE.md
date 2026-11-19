# Equivalência validada em 17/11/2026

O `pipeline_cetesb.py` foi executado com os 659 XLSX locais, em modo
`--dry-run`. Os resultados foram comparados integralmente com os artefatos
anteriores e com as tabelas publicadas no Azure SQL. Nenhuma tabela SQL foi
alterada durante esta validação.

## Resultado

| Tabela CETESB | Linhas | Colunas | Diferença numérica máxima |
|---|---:|---:|---:|
| features_coletas_wide | 39.750 | 568 | 0 |
| features_resumo_parametros | 45.336 | 25 | 0 |
| estudo_thm_base | 39.750 | 81 | 0 |
| predicoes_risco_thm | 39.750 | 50 | 0 |

Foram comparados todas as colunas, chaves, ausências e valores, incluindo
textos e datas. Também foram conferidos os tipos SQL e comprimentos dos
campos de texto; a collation herdada do banco não constitui diferença de
tipo declarado.

Os leitores Excel padrão e rápido produziram informações exatamente iguais
nos 659 arquivos, com 1.700.130 medições. O teste não ficou restrito a amostras.

Os arquivos Parquet efetivamente gravados pelos caminhos local e SQL têm
as mesmas **39.750 linhas e 111 colunas**, com igualdade exata dos valores,
tipos e ordenação. Embora o comparador de tabelas aceite tolerância numérica
de 1e-12, a diferença observada nas tabelas foi zero; a comparação final dos
Parquets em disco foi executada sem tolerância.

As tabelas ANA existentes também foram comparadas integralmente com o JSON
local: 503 captações e 544 relações candidatas, sem diferenças.

Passaram 22 testes automatizados. Os endpoints do mapa responderam HTTP 200.
O mapa mantém 39.749 coletas com coordenadas, sem excluir a coleta restante
das tabelas analíticas ou do Parquet.

## Ajustes que garantem a equivalência

- O `pipeline_cetesb.py` passou a utilizar a mesma orquestração por ponto de
  `reprocessar_local.py`, reduzindo também o consumo de memória. As regras
  científicas continuam definidas no próprio `pipeline_cetesb.py`.
- Os dois caminhos de publicação usam `pipeline_cetesb.sql_frame`, com
  tratamento comum de datas, Unicode, valores ausentes e tipos SQL.
- A consulta do mapa passou a incluir os 11 campos que faltavam em relação
  ao cache local, entre eles origem de PFTHM, estimativa histórica de THM,
  indicadores de excedência e contagens da triagem CONAMA.
- Colunas numéricas inteiramente NULL vindas do driver SQL mantêm seu tipo
  numérico, usando os metadados das tabelas. A gravação do Parquet preserva
  também o tipo textual canônico da data.
- O cache usa uma normalização comum de tipos, ordem de colunas e chaves.

## Execução normal

```powershell
python pipeline_cetesb.py
```

Esse comando processa os brutos, gera os artefatos em `resultados_v2/`,
atualiza o cache local e publica as tabelas brutas CETESB e as quatro tabelas
analíticas. O `.env` é lido na pasta do script.

Para atualizar somente as tabelas analíticas e o cache, mantendo as tabelas
brutas SQL existentes:

```powershell
python pipeline_cetesb.py --skip-raw
```

As duas tabelas ANA pertencem à integração externa e continuam sendo
mantidas por:

```powershell
python integrar_ana_etas.py --local-only --publish-db
```

`publicar_resultados_sql.py` é uma alternativa de publicação dos artefatos
locais já gerados. Ele publica as quatro tabelas CETESB e as duas ANA com
tabelas temporárias, backup e troca transacional, e recria o cache consultando
o SQL. Essas proteções adicionais não mudam o conteúdo analítico.

A equivalência pressupõe os mesmos arquivos e parâmetros. O padrão é não
substituir resultados censurados por uma fração do limite. Ambos os caminhos
aceitam o mesmo `--censor-factor` exploratório quando informado explicitamente.

## Repetir a validação sem alterar tabelas SQL

```powershell
python pipeline_cetesb.py --dry-run --output-dir resultados_equivalencia --cache-file resultados_equivalencia/cache_publicacao.parquet
python validar_equivalencia.py --check-readers --check-db
python -m unittest test_validacao -q
```

`--dry-run` gera arquivos locais; não publica no banco. Os diretórios separados
do exemplo evitam substituir os artefatos de uso corrente durante a comparação.
`validar_equivalencia.py` utiliza somente consultas SQL.

O relatório da execução concluída está em `resultados_v2/equivalencia.json`,
com contagens, diferenças observadas, hashes dos scripts e do cache.
Os artefatos anteriores ficaram em
`resultados_v2.backup_equivalencia_20261117_002827/`, e o cache anterior também
foi preservado. O cache de uso do mapa foi atualizado com as 111 colunas.

Esta verificação comprova equivalência de processamento e armazenamento.
Não muda as limitações científicas do IQA e dos modelos THM documentadas em
`METODOLOGIA_E_VALIDACAO.md`.
