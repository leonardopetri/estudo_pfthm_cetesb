# Tutorial de reprodução do estudo

Este tutorial leva do zero até os números e conclusões de `RESUMO_ARTIGO.md`:
aquisição dos brutos, processamento, avaliação das equações históricas do IPT,
recalibração do modelo de PFTHM e verificação automática dos resultados.

Há dois caminhos para obter os dados brutos:

- **Caminho A (recomendado para reprodução exata):** usar os 659 XLSX
  versionados em `ExtracoesCetesb.zip`. Garante os mesmos totais (1.700.130
  medições, 39.750 coletas) e os mesmos coeficientes.
- **Caminho B (reextração):** baixar novamente do InfoÁguas. Exige credenciais
  da CETESB e horas de execução; como o portal recebe dados novos, os totais e
  métricas **não** serão idênticos aos do resumo (ver seção 9).

Nenhuma etapa deste tutorial exige Azure SQL. A publicação em banco é opcional
e está descrita ao final.

## 1. Requisitos

- Python 3.11 (testado com 3.11.5) em Windows, Linux ou macOS.
- ~2 GB de disco (brutos descompactados ≈ 380 MB; Parquet ≈ 300 MB).
- Para o Caminho B: conta no InfoÁguas (CETESB) e Chromium do Playwright.
- Para publicação opcional: ODBC Driver 17 for SQL Server e uma connection string.

```powershell
git clone <url-do-repositorio> ExtracoesCetesb
cd ExtracoesCetesb
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
```

`requirements.txt` fixa versões (pandas 2.1.1, numpy 1.26.0, pyarrow, SQLAlchemy
etc.). Use exatamente essas versões: o ajuste numérico da recalibração e os
hashes dos artefatos dependem delas.

## 2. Obter os dados brutos

### Caminho A: brutos versionados

```powershell
Expand-Archive -Path ExtracoesCetesb.zip -DestinationPath . -Force
# Linux/macOS: unzip -o ExtracoesCetesb.zip
```

Os arquivos ficam na raiz com o padrão `CODIGO - 2010-2026.xlsx` (um por
ponto). Confira a contagem:

```powershell
(Get-ChildItem *.xlsx).Count      # esperado: 659
```

O processamento ignora arquivos cujo nome começa com `~$`, `consolidado` ou
`cetesb_dados_brutos`, e não lê a pasta `old files/` (extrações antigas de
2020–2025, mantidas apenas como histórico).

### Caminho B: reextração do InfoÁguas

```powershell
python -m playwright install chromium
Copy-Item .env.example .env
# edite .env e preencha INFOAGUAS_EMAIL e INFOAGUAS_SENHA
```

Extração completa (UGRHIs 01 a 22, 2010 até hoje, com pausas aleatórias entre
UGRHIs e log em `logs/`):

```powershell
python baixar_todas_ugrhis.py
```

Extração parcial para testar o ambiente:

```powershell
python scraper_infoaguas.py --list-ugrhis
python scraper_infoaguas.py --pontos PDIZ00700,PRAT02400 --data-inicio 01/01/2010
python scraper_infoaguas.py --ugrhi 06 --data-inicio 01/01/2010
```

O scraper divide o período em janelas de até cinco anos (limite do servidor),
valida ponto/datas/estrutura de cada XLSX, grava um manifesto com SHA-256 e só
substitui arquivos com `--overwrite` após validar o substituto. Execuções
interrompidas podem ser retomadas: arquivos com manifesto válido são pulados.
Se alguma UGRHI falhar, o script lista quais; basta rodar de novo.

## 3. Testes automatizados

Antes de processar, confirme que a implementação está íntegra. Os testes não
acessam portal, ANA nem banco; as regressões com `BILL02030` e `ABAN02790`
rodam se esses XLSX estiverem presentes.

```powershell
python -m unittest test_validacao -v
```

Esperado: todos os testes passam (30 na versão atual do código), incluindo
`RecalibrationTests`, que verifica o bloqueio de vazamento (alterar alvos dos
conjuntos de teste não altera coeficientes nem seleção).

## 4. Processar os brutos (seções 1–2 do resumo)

```powershell
python reprocessar_local.py --workers 4
```

Leva alguns minutos. Não lê `.env`, não faz requisições e não grava no Azure.
Produz `resultados_v2/` e `cache_mapa_sp.parquet`:

| Artefato | Conteúdo |
|---|---|
| `medicoes_tratadas/<PONTO>.parquet` | Tabela longa por ponto: valor original, qualificador, unidade original/padronizada |
| `features_coletas_wide.parquet` | 39.750 coletas × 568 colunas |
| `features_resumo_parametros.parquet` | Resumo por parâmetro (n total, n usado, censura inferior/superior) |
| `estudo_thm_base.parquet` | Base do estudo THM (39.750 × 81) |
| `predicoes_risco_thm.parquet` | Equações IPT históricas e campos de risco (probabilidades nulas) |
| `cache_mapa_sp.parquet` | Cache geoespacial do mapa |
| `manifesto.json` | SHA-256 de fontes e scripts, unidades, totais, versão |

Ao final o script imprime `manifesto["totais"]`. Com o Caminho A, esperado:

```json
{
  "arquivos_brutos": 659, "medicoes": 1700130, "coletas": 39750, "pontos": 659,
  "nitrogenio_total_medido": 4433, "nitrogenio_total_soma_completa": 13519,
  "nitrogenio_total_indeterminado": 21798, "probabilidades_thm_publicadas": 0
}
```

Se houver duplicidade por ponto/data/hora/parâmetro, data inválida ou unidades
incompatíveis, a execução **interrompe** em vez de agregar. Isso é intencional.
Resultados anteriores são preservados em `resultados_v2.backup_<data>/` e
`cache_mapa_sp.parquet.backup_<data>`.

Não use `--censor-factor`: ele substitui limites de quantificação por frações
do limite, é exploratório e não faz parte do estudo publicado.

## 5. Avaliar as equações históricas do IPT (seção 2.6)

```powershell
python avaliar_modelos_ipt.py
```

Lê `resultados_v2/predicoes_risco_thm.parquet` e grava
`Doc/pipeline/avaliacao_modelos_ipt.json` com as métricas pós-2012. Valores
esperados (Caminho A):

| Avaliação | n | R² |
|---|---:|---:|
| Modelo 1 em ANOR02300 | 16 | −0,169 |
| Modelo 2 em ARPE02800 | 54 | −0,835 (9 previsões negativas) |
| Modelo 1 transferido aos demais pontos | 4.753 | 0,039 |
| Modelo 2 transferido | 4.715 | colapso: 2.647 negativas, RMSE > 21.000 µg/L |

Esse resultado motiva a seção 3 do resumo (recalibração).

## 6. Recalibrar o modelo de PFTHM (seção 3)

```powershell
python -m unittest test_validacao.RecalibrationTests -v
python recalibrar_pfthm.py
```

O script lê `resultados_v2/estudo_thm_base.parquet`, exige
`versao_dados == 2026-11-17-v4-ipt-cod` e cria um diretório novo
`resultados_recalibracao/pfthm_recal_<UTC>/` com:

- `predicoes_pfthm_recalibradas.parquet` (39.750 × 23);
- `modelos_e_avaliacao.json` (coeficientes, partições, validação, testes,
  hashes da fonte e do código, versões das dependências).

O protocolo é determinístico: a partição dos pontos usa SHA-256 do código com
prefixo `pfthm-recal-v1:` (módulo 5), e os cortes temporais são fixos
(treino ≤ 2020, validação 2021–2022, teste ≥ 2023). Portanto, com a mesma base
e as mesmas versões de bibliotecas, os coeficientes são idênticos:

```text
PFTHM = 152,945813 + 45,810177·COD − 0,280725389·COD²     (COD em mg/L, 1–159)
```

O terminal imprime `modelo_selecionado` (esperado `modelo2`), as partições
(2.214/451/657/3.322/1.105/1.170 coletas) e as métricas dos testes reservados
(quadrático: RMSE 188,09 temporal e 204,25 espacial; R² ≈ 0,29 em ambos).

O identificador da execução (`pfthm_recal_<UTC>`) muda a cada rodada; isso é
esperado e não afeta coeficientes nem métricas.

## 7. Verificar que as conclusões foram reproduzidas

```powershell
python verificar_conclusoes.py
```

O script recalcula, a partir dos artefatos locais, todos os números citados no
resumo e compara com os valores esperados:

- totais de aquisição/processamento (seções 1–2);
- métricas das equações IPT (2.6);
- partições, critério de seleção, coeficientes de faixa e métricas dos testes da
  recalibração (3.2–3.5);
- distribuição do PFTHM observado: n = 5.905 em 120 pontos, mediana 294,
  P75/P90/P95/P99 = 428/588/703/1.060, máximo 2.650, frações acima de
  100/373/400/461 µg/L = 95,9/33,4/28,8/20,9 %, 14 pontos com mediana > 400
  entre 98 com n ≥ 20, os dez pontos de maior mediana e as medianas anuais (4.1);
- previsões recalibradas utilizáveis: 13.888 em 502 pontos, 24,7 % > 400,
  mediana 332, P75 398 (4.1);
- ausência de THM observado na ETA e de probabilidades de risco (2.6, 4.3).

Saída esperada: `64/64 verificações conferem.` e código de saída 0. Cada linha
mostra `obtido` e `esperado`; qualquer `[DIVERGE]` indica que a base ou o
ambiente diferem da execução de referência. Para apontar uma execução de
recalibração específica: `--recalibracao resultados_recalibracao/pfthm_recal_...`.

As conclusões qualitativas (4.2 e 4.3) seguem diretamente desses números:

1. **Pressão elevada e disseminada de precursores**: 28,8 % das amostras acima
   de 400 µg/L; em 14 mananciais isso é a mediana.
2. **COD isolado é preditor insuficiente**: equações históricas com R² ≤ 0,05
   fora do ajuste; modelo recalibrado explica ~29 % da variância e tem RMSE da
   ordem da distância entre a mediana (294) e o limiar (400).
3. **Não se conclui risco ao consumidor**: `thm_eta_observado_ug_l` é nulo em
   todas as linhas; a conversão PFTHM → THM é uma reta de 2012 não recalibrada.

## 8. Etapas opcionais

### ETAs da ANA (seção 1)

Sem requisições, reutilizando o JSON versionado (`ana_etas_sp.json`, 503
captações, 544 relações candidatas a ≤ 15 km):

```powershell
python integrar_ana_etas.py --local-only
```

Para consultar a API da ANA novamente, omita `--local-only`. As relações
geradas são hipóteses (`vinculo_confirmado = falso`).

### Mapa interativo

```powershell
python app_mapa_sp.py        # abre em http://127.0.0.1:5050 (variável PORT altera)
```

Usa `cache_mapa_sp.parquet`; rejeita caches de versões anteriores. A camada
padrão mostra PFTHM medido ou, na ausência, o quadrático recalibrado da mesma
coleta (`modelo_pfthm_mapa.py`, coeficientes congelados; não retreina).

### Publicação em Azure SQL (seções 2.7 e 3.6)

Preencha `AZURE_SQL_CONNECTION_STRING` em `.env` e execute, nesta ordem:

```powershell
python pipeline_cetesb.py --dry-run     # revise antes
python pipeline_cetesb.py
python recalibrar_pfthm.py --publicar-sql
python validar_equivalencia.py --check-db
```

`pipeline_cetesb.py` substitui as tabelas (`if_exists="replace"`);
`recalibrar_pfthm.py --publicar-sql` publica `dbo.predicoes_pfthm_recalibradas`
por tabela temporária com conferência integral e preserva a versão anterior em
schema de backup. `publicacao_sql.json` registra a verificação.

## 9. O que esperar com dados reextraídos (Caminho B)

Com brutos baixados em outra data, os seguintes valores mudam e o
`verificar_conclusoes.py` acusará divergências numéricas — isso **não**
invalida a reprodução, desde que:

- `manifesto.json` reporte 659 pontos ou mais, sem interrupção por duplicidade;
- a avaliação IPT continue com R² ≤ 0,05 fora do ajuste e negativo nos pontos
  de origem;
- a recalibração continue selecionando `modelo2` (quadrático) e o R² nos testes
  permaneça em torno de 0,3, com RMSE da ordem de 190–210 µg/L;
- a fração de PFTHM observado acima de 400 µg/L permaneça na faixa de ~25–35 %.

Se o portal passar a entregar dados de 2027 em diante, o teste temporal
(≥ 2023) ganha observações e as métricas se deslocam; o protocolo de partição,
porém, não muda. Documente a data da extração e preserve o `manifesto.json`.

## 10. Erratas corrigidas na verificação

Dois números da seção 4.1 do `RESUMO_ARTIGO.md` foram ajustados após a
conferência com os artefatos (sem alteração das conclusões):

| Redação anterior | Redação corrigida |
|---|---|
| "com 54–81% das amostras acima de 400 µg/L" (dez pontos listados) | 58–81 %; o valor 54 % pertence a PEBA00900, 11º ponto, fora da lista |
| "mediana anual ... 308–383 µg/L (2018–2026)" | "308–383 µg/L (2018–2026, exceto 2024, com 280 µg/L)" |

## 11. Resolução de problemas

| Sintoma | Causa provável | Ação |
|---|---|---|
| `Nenhum arquivo bruto encontrado.` | XLSX não estão na raiz | Descompacte o zip na raiz; não use subpasta |
| `Chaves de coleta duplicadas` / `Unidades incompatíveis` | Arquivo duplicado para o mesmo ponto (ex.: cópia manual) | Remova a cópia; o processamento não agrega |
| `Base com versão incompatível.` na recalibração | `estudo_thm_base.parquet` de outra versão do pipeline | Rode `reprocessar_local.py` com este código |
| `UnicodeEncodeError` no console Windows | Console cp1252 | `$env:PYTHONIOENCODING="utf-8"` ou use Windows Terminal |
| Falha no login do scraper | Credenciais ausentes/erradas no `.env` | Confira `INFOAGUAS_EMAIL`/`INFOAGUAS_SENHA` |
| Mapa recusa o cache | Cache de versão antiga | Rode `reprocessar_local.py` novamente |
