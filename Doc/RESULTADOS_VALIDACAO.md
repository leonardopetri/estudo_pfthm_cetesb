# Resultado da execução de 16/11/2026

Versão: `2026-11-16-v2`.

## Artefatos locais

O reprocessamento de 659 arquivos XLSX terminou com 1.700.130 medições,
39.750 coletas e 659 pontos. As 39.750 chaves de coleta coincidem com as do
cache anterior: nenhuma chave foi perdida ou acrescentada.

Os arquivos brutos foram preservados. Seus 659 SHA-256 foram conferidos
contra o manifesto, assim como os hashes dos três scripts de processamento.
O cache anterior e o JSON ANA anterior têm backups na raiz.

Artefatos em `resultados_v2/`:

- `medicoes_tratadas/`: uma partição Parquet por ponto, com valores originais,
  unidades, qualificadores, limites e valores analíticos.
- `features_coletas_wide.parquet`: 39.750 coletas e 568 colunas.
- `features_resumo_parametros.parquet`.
- `estudo_thm_base.parquet`.
- `predicoes_risco_thm.parquet`.
- `cache_mapa_sp.parquet`.
- `manifesto.json`: fontes, hashes, unidades, versão e totais.

O cache de uso do aplicativo, na raiz, foi atualizado.

## Resultados conferidos

| Caso | Resultado anterior | Resultado corrigido |
|---|---|---|
| BILL02030, 14/01/2016, nitrogênio total | 0,41 mg/L | 2,40 mg/L, origem **Medido** |
| BILL02030, 28/01/2010, IET | 67,37, Hipereutrófico | 61,97, Eutrófico |
| Probabilidade THM ausente convertida em risco zero | 1.622 casos na revisão | 0 casos |
| Ensaios TA98/TA100 + S9 e − S9 | Colunas colidiam após normalização | Colunas distintas |
| Condutividade em mS/cm e µS/cm | Escalas distintas | Padronização para µS/cm |
| Relações ANA acima de 15 km | Produzidas pelo método anterior | Não publicadas |

Nitrogênio total: 4.433 medições exatas diretas; 13.519 somas completas;
21.798 resultados indeterminados. A maior quantidade de nulos é intencional:
componentes ausentes e limites de detecção não são concentrações exatas.

Nenhuma probabilidade THM foi publicada como validada. As colunas legadas
de risco e THM observado na ETA permanecem nulas. A estimativa histórica
de THM fica identificada separadamente.

O mapa contém 658 pontos e 39.749 coletas com coordenadas disponíveis.
O conjunto analítico mantém a coleta sem coordenadas. O JSON ANA contém
503 captações e 544 relações candidatas, todas em até 15 km e sem declaração
de conexão hídrica comprovada. Não houve nova consulta à ANA nessa execução;
foram reutilizados seus dados locais.

## Verificações executadas

- 17 testes automatizados passaram.
- Sintaxe Python conferida e JavaScript dos dois HTML validado com Node.
- Página inicial e endpoints `/api/init` e `/api/station/BILL02030`: HTTP 200.
- Navegação real com Chromium/Playwright: seleção da coleta de regressão,
  nitrogênio total exibido como 2,40, risco indeterminado, opções de modelos
  desabilitadas, ausência de extrapolação geográfica por padrão, seleção por
  data e hora e nenhum erro JavaScript.
- Integridade das fontes e consistência das chaves verificadas.

## Consulta ao banco

A conexão do `.env` foi utilizada sem expor credenciais, exclusivamente em
consultas. Foram identificadas 670 tabelas; a seleção por esquema do
consolidador retornou 664 tabelas brutas e excluiu as tabelas ANA.

O banco ainda contém os resultados anteriores. O exemplo de nitrogênio
total de BILL02030 em 14/01/2016 continua em 0,41 mg/L no banco. Não foram
encontradas colunas de UV254 medido na tabela de features.

Na etapa inicial acima, nenhuma tabela SQL foi alterada. A publicação
posterior, autorizada pelo usuário, está registrada a seguir.

As dependências científicas dos modelos, os limites da triagem CONAMA e a
política de censura estão em `METODOLOGIA_E_VALIDACAO.md`. Os testes verificam
a implementação; não representam validação científica independente dos
modelos nem atualização/completude perante o portal InfoÁguas.

## Publicação posterior no Azure SQL

As seis tabelas derivadas foram publicadas em `dbo` com os resultados locais
validados, por `publicar_resultados_sql.py`:

| Tabela | Linhas |
|---|---:|
| features_coletas_wide | 39.750 |
| features_resumo_parametros | 45.336 |
| estudo_thm_base | 39.750 |
| predicoes_risco_thm | 39.750 |
| ana_captacoes_etas_sp | 503 |
| relacao_cetesb_ana_etas | 544 |

A carga ocorreu em tabelas temporárias, conferidas antes da troca
transacional. As versões anteriores ficaram preservadas no schema
`cetesb_backup_20261116_233427`. As tabelas brutas não foram substituídas.

O arquivo `cache_mapa_sp.parquet` foi recriado por consulta ao banco já
atualizado. Foram confirmadas 39.750 coletas no Parquet, sem chaves duplicadas,
e 39.749 coletas com coordenadas no mapa. Página e endpoints responderam HTTP
200. Nitrogênio de 2,40 mg/L e IET de 61,97 nos exemplos de BILL02030 foram
conferidos no banco. Os resultados de risco não validados permanecem nulos.

Recibo com contagens, schema de backup e SHA-256 do Parquet:
`resultados_v2/publicacao_sql_20261116_233427.json`.
