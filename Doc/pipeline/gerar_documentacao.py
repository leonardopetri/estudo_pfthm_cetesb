import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import html
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import openpyxl
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pipeline_cetesb import DATA_VERSION, COLLECTION_KEY, CORE_PARAMETERS, sanitize_table_name, slugify
from modelo_pfthm_mapa import MODEL_VERSION
from regras_documentais import DESCRIPTIONS, NULL_COLUMNS, REL_ALIASES, RULES, THM_ALIASES

SOURCES = {p.name: p.read_text(encoding="utf-8-sig") for p in ROOT.glob("*.py")}
TREES = {f: ast.parse(s) for f, s in SOURCES.items()}
HASHES = {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in SOURCES}
AST_ASSIGNMENTS = defaultdict(lambda: defaultdict(list))
NAMED_ASSIGNMENTS = defaultdict(lambda: defaultdict(list))
FUNCTIONS = defaultdict(list)
for source_file, tree in TREES.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            FUNCTIONS[source_file].append(node)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    NAMED_ASSIGNMENTS[source_file][target.id].append(node)
                if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str):
                    AST_ASSIGNMENTS[source_file][target.slice.value].append((node.lineno, node.end_lineno, node.value))
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    AST_ASSIGNMENTS[source_file][key.value].append((key.lineno, value.end_lineno, value))
RULE_BY_ID = {r[0]: r for r in RULES}
RAW_COLUMNS = [
    "periodo_de", "periodo_ate", "cod_interaguas", "tipo_rede", "ugrhi", "codigo_ponto",
    "status_ponto", "data_coleta", "hora_coleta", "parametro", "sinal", "valor", "unidade",
    "tipo_parametro", "sistema_hidrico", "tipo_de_sistema_hidrico", "classe", "municipio",
    "uf", "inicio_operacao", "fim_operacao", "latitude", "longitude", "altitude", "localizacao", "captacao",
]
TECH_COLUMNS = ["abaixo_limite_deteccao", "valor_numerico", "altitude_m", "latitude_decimal", "longitude_decimal", "arquivo_origem"]
DATE_COLUMNS = {"periodo_de", "periodo_ate", "data_coleta", "inicio_operacao", "fim_operacao", "primeira_coleta", "ultima_coleta"}
META_COLUMNS = ["codigo_ponto", "data_coleta", "hora_coleta", "cod_interaguas", "tipo_rede", "ugrhi",
                "status_ponto", "sistema_hidrico", "tipo_de_sistema_hidrico", "classe", "municipio", "uf",
                "latitude_decimal", "longitude_decimal", "altitude_m", "localizacao", "captacao"]
SUMMARY_KEY = ["codigo_ponto", "sistema_hidrico", "classe", "municipio", "tipo_parametro", "parametro", "unidade"]
ANALYTIC = ["features_coletas_wide", "features_resumo_parametros", "estudo_thm_base", "predicoes_risco_thm"]
PURPOSES = {
    "ingestao_excel": ("Resultado de um parâmetro por coleta, após limpeza inicial.", "codigo_ponto, data_coleta, hora_coleta, parametro (unicidade exigida posteriormente)", "Excel CETESB → leitura/limpeza → tabelas SQL por arquivo"),
    "medicoes_tratadas": ("Resultado de um parâmetro por coleta, com unidade e censura preservadas.", "codigo_ponto, data_coleta, hora_coleta, param_slug", "Excel limpo → prepare_measurements → partição Parquet por ponto"),
    "features_coletas_wide": ("Uma coleta por ponto, data e hora; parâmetros e variáveis derivadas em colunas.", ", ".join(COLLECTION_KEY), "Medições → pivôs/índices/séries → enriquecimento THM → Parquet e SQL"),
    "features_resumo_parametros": ("Um grupo de resultados por ponto e dimensões cadastrais/analíticas.", ", ".join(SUMMARY_KEY), "Medições → estatísticas → Parquet e SQL"),
    "estudo_thm_base": ("Uma coleta com covariáveis do estudo de formação de THM.", ", ".join(COLLECTION_KEY), "Wide → seleção/derivação → Parquet e SQL"),
    "predicoes_risco_thm": ("Uma coleta com PFTHM medido, duas estimativas IPT COD, seleção/proveniência e risco probabilístico indisponível.", ", ".join(COLLECTION_KEY), "Wide → duas equações COD em todos os pontos elegíveis → seleção e status → Parquet e SQL"),
    "predicoes_pfthm_recalibradas": ("Uma coleta com previsões recalibradas, modelo selecionado e papel na avaliação da execução congelada.", ", ".join(COLLECTION_KEY), "Base THM → ajuste/validação/testes separados → coeficientes e previsões → Parquet e tabela SQL independente"),
    "cache_mapa_sp": ("Uma coleta com união da base THM e colunas exclusivas de predições.", ", ".join(COLLECTION_KEY), "Base THM + predições → inner join 1:1 → cache canônico"),
    "ana_captacoes_etas_sp": ("Um registro de captação ANA enriquecido com cadastro de ETA quando há match.", "id_captacao_ana (validar unicidade observada)", "API ANA ou JSON anterior → integração → JSON e SQL"),
    "relacao_cetesb_ana_etas": ("Uma captação candidata escolhida por ponto CETESB, quando existe candidata até 15 km.", "codigo_ponto; id_captacao_ana relaciona cadastro ANA", "Último ponto com coordenadas + ANA → score geográfico/textual → JSON e SQL"),
    "pontos_monitoramento": ("Uma linha do catálogo de pontos usado pela extração; arquivo local é subconjunto histórico.", "codigo_ponto / cod_interaguas; não representa inventário completo atual", "Catálogo HTML InfoÁguas → renomeação → CSV"),
    "exportacao_brutos_excel": ("Exportação histórica de linhas de ingestão em abas; não é nova entidade analítica.", "Mesma granularidade da ingestão; sem unicidade adicional garantida", "Ingestão local ou SQL → exportação XLSX por lotes/abas"),
}
PROFILES = {}
PARAMETERS = defaultdict(set)
UNITS = defaultdict(set)
SCHEMAS = {}
PHYSICAL = []
ISSUES = []
HEADERS = {}
EXCEL_FILES = {}
RAW_PROFILES = {}
SCHEMA_TYPES = defaultdict(lambda: defaultdict(set))
FRAMES = {}


def issue(identifier, scope, detail, action, severity="Revisão"):
    ISSUES.append({"id": identifier, "escopo": scope, "gravidade": severity,
                   "constatacao": detail, "acao": action, "status": "Aberta"})


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def text_value(value):
    if value is None or pd.isna(value):
        return "NULL"
    if isinstance(value, (bool, np.bool_)):
        return "true" if value else "false"
    return str(value)


def schema_id(fields):
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    key = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    SCHEMAS[key] = fields
    return key


def profile(frame, table, origin, aggregate=False):
    records = PROFILES.setdefault(table, {}) if aggregate else {}
    for col in frame:
        s = frame[col]
        v = s.dropna()
        info = records.setdefault(col, {"total": 0, "nulos": 0, "exemplo": "NULL", "local_exemplo": "",
                                        "dominio": set(), "dominio_limitado": False, "minimo": None, "maximo": None})
        info["total"] += len(s)
        info["nulos"] += int(s.isna().sum())
        if len(v):
            if info["exemplo"] == "NULL":
                idx = v.index[0]
                keys = {k: text_value(frame.loc[idx, k]) for k in COLLECTION_KEY if k in frame}
                info["exemplo"] = text_value(v.iloc[0])
                info["local_exemplo"] = f"{origin}; linha de dados {frame.index.get_loc(idx) + 1}; {json.dumps(keys, ensure_ascii=False)}"
            unique = v.astype(str).unique()
            if len(unique) > 32 or len(info["dominio"] | set(unique[:33])) > 32:
                info["dominio_limitado"] = True
            info["dominio"].update(str(x) for x in unique[:32])
            info["dominio"] = set(sorted(info["dominio"])[:32])
            if pd.api.types.is_numeric_dtype(s):
                low, high = float(v.min()), float(v.max())
                info["minimo"] = low if info["minimo"] is None else min(low, info["minimo"])
                info["maximo"] = high if info["maximo"] is None else max(high, info["maximo"])
    if not aggregate:
        PROFILES[table] = records
    return records


def register(path, kind, table, fields=None, rows=None, suffix="", sha=None):
    stat = path.stat()
    PHYSICAL.append({"objeto": path.relative_to(ROOT).as_posix() + suffix, "classe": kind,
                     "tabela_logica": table, "linhas": rows, "quantidade_colunas": len(fields) if fields else None,
                     "esquema_id": schema_id(fields) if fields else "", "bytes": stat.st_size,
                     "modificado_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                     "sha256": sha or digest(path)})


def excel_headers(path):
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    result = []
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            with z.open("xl/sharedStrings.xml") as stream:
                for _, elem in ET.iterparse(stream, events=("end",)):
                    if elem.tag.endswith("}si"):
                        shared.append("".join(elem.itertext()))
                        elem.clear()
        relationships = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.attrib["Id"]: r.attrib["Target"] for r in relationships}
        workbook_xml = ET.fromstring(z.read("xl/workbook.xml"))
        for sheet in workbook_xml.findall("m:sheets/m:sheet", ns):
            rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            target = targets[rid]
            target = target.lstrip("/") if target.startswith("/") else "xl/" + target
            header, total = [], None
            with z.open(target) as stream:
                for _, elem in ET.iterparse(stream, events=("end",)):
                    if elem.tag.endswith("}dimension"):
                        match = re.search(r"(\d+)$", elem.attrib.get("ref", ""))
                        total = int(match.group(1)) - 1 if match else None
                    if elem.tag.endswith("}row"):
                        for cell in elem.findall("m:c", ns):
                            letters = re.match(r"[A-Z]+", cell.attrib["r"]).group()
                            index = openpyxl.utils.column_index_from_string(letters) - 1
                            header.extend([None] * max(0, index + 1 - len(header)))
                            value = cell.find("m:v", ns)
                            if cell.attrib.get("t") == "s" and value is not None:
                                header[index] = shared[int(value.text)]
                            elif cell.attrib.get("t") == "inlineStr":
                                header[index] = "".join(t.text or "" for t in cell.findall(".//m:t", ns))
                            elif value is not None:
                                header[index] = value.text
                        break
            result.append((sheet.attrib["name"], tuple(header), total))
    return result


def inventory():
    manifest = json.loads((ROOT / "resultados_v2/manifesto.json").read_text(encoding="utf-8"))
    manifest_sources = {x["arquivo"]: x for x in manifest["fontes"]}
    for f, expected in manifest["codigo_sha256"].items():
        if HASHES.get(f) != expected:
            issue("HASH-CODIGO-" + f, f, "Código atual diverge do hash registrado no manifesto.", "Não atribuir automaticamente artefatos ao código atual.", "Alta")
    paths = [p for p in ROOT.rglob("*") if p.is_file() and
             not any(x in p.relative_to(ROOT).parts for x in ("Doc", ".devin", "__pycache__", ".git"))]
    for index, path in enumerate(sorted(paths), 1):
        rel = path.relative_to(ROOT).as_posix()
        name = path.name
        historical = "backup" in rel or ".antes_" in rel or rel.startswith("old files/")
        if ".parquet" in name:
            pf = pq.ParquetFile(path)
            fields = [{"coluna": f.name, "tipo": str(f.type), "aceita_nulo": f.nullable} for f in pf.schema_arrow]
            table = "medicoes_tratadas" if path.parent.name == "medicoes_tratadas" else name.split(".")[0]
            if table == "cache_publicacao":
                table = "cache_mapa_sp"
            kind = "Backup" if historical else ("Partição" if table == "medicoes_tratadas" else "Artefato atual / réplica")
            register(path, kind, table, fields, pf.metadata.num_rows)
            if not historical:
                for f in fields:
                    SCHEMA_TYPES[table][f["coluna"]].add(f["tipo"])
                if table == "medicoes_tratadas":
                    df = pd.read_parquet(path)
                    profile(df, table, rel, aggregate=True)
                    local = profile(df[RAW_COLUMNS + TECH_COLUMNS], "_raw", rel)
                    RAW_PROFILES[path.stem] = local
                    for param, group in df.groupby("param_slug", dropna=True):
                        PARAMETERS[param].update(group.parametro.dropna().astype(str).unique())
                        UNITS[param].update(group.unidade_padronizada.dropna().astype(str).unique())
                elif path.parent.name == "resultados_v2" and name != "cache_publicacao.parquet":
                    frame = pd.read_parquet(path)
                    FRAMES[table] = frame
                    profile(frame, table, rel)
        elif path.suffix == ".xlsx":
            file_hash = digest(path)
            for sheet_name, header, row_count in excel_headers(path):
                fields = [{"coluna": str(v) if v is not None else f"col_{i}", "tipo": "célula Excel; tipo varia por célula"} for i, v in enumerate(header)]
                table = "exportacao_brutos_excel" if name.startswith("cetesb_dados_brutos") else "ingestao_excel"
                kind = "Exportação histórica" if table.startswith("exportacao") else ("Arquivo histórico" if historical else "Fonte Excel")
                register(path, kind, table, fields, row_count, "#" + sheet_name, file_hash)
                if table == "exportacao_brutos_excel":
                    for field in fields:
                        SCHEMA_TYPES[table][field["coluna"]].add(field["tipo"])
                if table == "ingestao_excel":
                    EXCEL_FILES[sanitize_table_name(str(path))] = {"arquivo": rel, "aba": sheet_name, "headers": header,
                                                                  "atual": not historical}
                    for h in header:
                        if h is not None:
                            HEADERS.setdefault(slugify(h), set()).add(str(h))
            if name in manifest_sources and manifest_sources[name]["sha256"] != file_hash:
                issue("HASH-FONTE-" + name, rel, "Arquivo diverge do manifesto analítico.", "Reconciliar a fonte antes de atribuir equivalência.", "Alta")
        elif name.startswith("ana_etas_sp.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            for table, items in [("ana_captacoes_etas_sp", data["ana_etas"]),
                                 ("relacao_cetesb_ana_etas", list(data["relations_by_station"].values()))]:
                frame = pd.DataFrame(items)
                fields = [{"coluna": c, "tipo": f"JSON; pandas {frame[c].dtype}"} for c in frame]
                register(path, "Backup JSON" if historical else "Coleção JSON", table, fields, len(frame), "#" + table)
                if not historical:
                    FRAMES[table] = frame
                    profile(frame, table, rel + "#" + table)
                    for f in fields:
                        SCHEMA_TYPES[table][f["coluna"]].add(f["tipo"])
        elif path.suffix == ".csv":
            frame = pd.read_csv(path, dtype=str)
            fields = [{"coluna": c, "tipo": "texto CSV"} for c in frame]
            register(path, "Catálogo auxiliar", path.stem, fields, len(frame))
            if path.stem == "pontos_monitoramento":
                FRAMES[path.stem] = frame
                profile(frame, path.stem, rel)
                for f in fields:
                    SCHEMA_TYPES[path.stem][f["coluna"]].add(f["tipo"])
        elif path.suffix == ".json":
            register(path, "Metadado de execução / recibo", "metadados_execucao")
        if index % 200 == 0:
            print(f"Inventário: {index}/{len(paths)} arquivos examinados.", flush=True)
    PROFILES.pop("_raw", None)
    for c in RAW_COLUMNS + TECH_COLUMNS:
        SCHEMA_TYPES["ingestao_excel"][c] = SCHEMA_TYPES["medicoes_tratadas"][c].copy()
    PROFILES["ingestao_excel"] = {c: PROFILES["medicoes_tratadas"][c] for c in RAW_COLUMNS + TECH_COLUMNS}
    issue("ESCOPO-EXCEL", "Fontes / extração", "Arquivos locais anteriores à versão atual do scraper não provam execução dessas regras; não foram baixados novamente.", "Confirmar completude no portal e manifestos de download; regras atuais não reconstituem histórico ausente.")
    issue("ANA-FONTE", "ANA", "JSON reutilizado de extração anterior; respostas originais da API e instante de extração não estão disponíveis no JSON atual.", "Confirmar domínio de atributos e proveniência com metadados ANA; não tratar a regra atual como evidência da extração histórica.")
    issue("SQL-VALORES", "SQL", "Nesta execução foram consultados metadados, não valores de todas as tabelas. Contagens sys.partitions são metadados, não COUNT_BIG atual.", "Usar equivalencia.json como evidência histórica; repetir comparação de conteúdo se necessário.")
    issue("TIPOS-PARTICOES", "medicoes_tratadas", "Tipos observados por coluna podem variar entre string e timestamp, especialmente datas inteiramente ausentes.", "Consultar Objetos e Esquemas; não impor um único tipo Parquet a todas as partições.")
    issue("DOMINIO", "Catálogo CETESB / ANA", "Domínios observados não são listas oficiais de valores permitidos. Unidade original e descrição do ensaio são preservadas sem validar protocolo laboratorial.", "Revisão pelo responsável de domínio; não inferir equivalência entre UFC/NMP ou bases químicas somente pelo nome.")
    return manifest


def literal(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def assignment(file, column, start, end):
    candidates = [entry for entry in AST_ASSIGNMENTS[file][column] if start <= entry[0] <= end]
    return min(candidates, default=None, key=lambda x: x[0])


def function_scope(file, line):
    scopes = [node for node in FUNCTIONS[file] if node.lineno <= line <= node.end_lineno]
    return max((node.lineno for node in scopes), default=0)


def refs(node, file, before, seen=None):
    seen = set() if seen is None else seen
    result = set()
    for part in ast.walk(node):
        if isinstance(part, ast.Subscript) and isinstance(part.value, ast.Name):
            key = literal(part.slice)
            if key and part.value.id in {"w", "base", "df", "df_wide", "st", "ana_row", "predicoes_df"}:
                result.add((part.value.id, key))
        if isinstance(part, ast.Call):
            if isinstance(part.func, ast.Name) and part.func.id in {"get_col", "col_or_nan"} and part.args:
                key = literal(part.args[0])
                if key:
                    result.add(("w" if part.func.id == "col_or_nan" else "base", key))
            if isinstance(part.func, ast.Attribute) and part.func.attr == "get" and isinstance(part.func.value, ast.Name):
                if part.func.value.id in {"base", "w", "st", "ana_row"} and part.args and literal(part.args[0]):
                    result.add((part.func.value.id, literal(part.args[0])))
        if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load) and (part.id, before) not in seen:
            seen.add((part.id, before))
            scope = function_scope(file, before)
            values = [entry for entry in NAMED_ASSIGNMENTS[file][part.id]
                      if entry.lineno < before and function_scope(file, entry.lineno) == scope]
            if values:
                entry = max(values, key=lambda x: x.lineno)
                result |= refs(entry.value, file, entry.lineno, seen)
    return result


RECAL_CACHE_COLUMNS = {
    "pfthm_recalibrado_bruto_ug_l", "pfthm_recalibrado_ug_l",
    "ponto_no_ajuste_recalibrado_bin", "fora_faixa_cod_recalibrado_bin",
    "status_modelo_recalibrado", "pfthm_mapa_ug_l", "origem_pfthm_mapa",
    "thm_eta_mapa_estimado_ug_l", "versao_modelo_recalibrado", "versao_cache_mapa",
}


def get_rule(table, col):
    if table == "ingestao_excel":
        return {"valor_numerico": "ING-NUMERO", "altitude_m": "ING-NUMERO",
                "latitude_decimal": "ING-COORD", "longitude_decimal": "ING-COORD"}.get(col, "ING-LIMPEZA")
    if table == "medicoes_tratadas":
        return "MED-PARTICAO" if col in RAW_COLUMNS + TECH_COLUMNS else "MED-PREPARO"
    if table == "features_resumo_parametros":
        return "RES-ESTATISTICA"
    if table == "estudo_thm_base":
        return "THM-SELECAO"
    if table == "predicoes_risco_thm":
        return "THM-RESULTADOS"
    if table == "predicoes_pfthm_recalibradas":
        return "RECAL-PUBLICACAO" if col in {"versao_modelo", "versao_dados_origem"} else "RECAL-MODELOS"
    if table == "ana_captacoes_etas_sp":
        return "ANA-LEGADO" if col == "origem_coordenada_planta" else "ANA-EXTRACAO"
    if table == "relacao_cetesb_ana_etas":
        return "ANA-RELACAO"
    if table == "cache_mapa_sp":
        return "CACHE-RECAL" if col in RECAL_CACHE_COLUMNS else "CACHE-JUNCAO"
    if table == "pontos_monitoramento":
        return "EXT-CATALOGO"
    if table == "exportacao_brutos_excel":
        return "EXPORT-EXCEL"
    if col in META_COLUMNS:
        return "WIDE-META"
    if col in {"qtd_parametros_medidos", "qtd_parametros_censurados", "perc_parametros_censurados"}:
        return "WIDE-CONTAGEM"
    for rule, a, b in [
        ("WIDE-TEMPO", 424, 435), ("WIDE-CONTAGEM", 437, 448),
        ("WIDE-CATEGORIA", 460, 499), ("WIDE-QUIMICA", 501, 542),
        ("WIDE-IET", 544, 558), ("WIDE-IQA-NULO", 560, 564),
        ("WIDE-CONAMA", 90, 144), ("WIDE-SERIE", 568, 591),
        ("THM-RESULTADOS", 919, 926),
    ]:
        if assignment("pipeline_cetesb.py", col, a, b):
            return "WIDE-IQA" if col == "iqa_experimental" else rule
    if col.startswith(("lag1_", "delta_", "mm4_")):
        return "WIDE-SERIE"
    return "WIDE-PIVOT"


def source_description(col):
    alias = THM_ALIASES.get(col, REL_ALIASES.get(col))
    if alias:
        return DESCRIPTIONS.get(alias) or source_description(alias)
    if col in DESCRIPTIONS:
        return DESCRIPTIONS[col]
    if col in NULL_COLUMNS:
        return "Campo legado reservado a resultado de modelo, probabilidade ou classificação THM não validado; não é calculado nem substituído por zero."
    if col.startswith("violacao_"):
        names = {"od": "oxigênio dissolvido", "dbo": "DBO", "turbidez": "turbidez", "fosforo": "fósforo",
                 "amonia": "nitrogênio amoniacal", "ph": "pH", "ecoli": "E. coli"}
        return f"Triagem do limite de {names.get(col[9:], col[9:])}: 1=violação inequívoca, 0=não violação avaliada, nulo=indeterminado."
    if col in {"i_1_jfm", "i_2_amj", "i_3_jas", "i_4_ond"}:
        return f"Indicador binário de coleta no trimestre {col[2]}; 1 quando pertence ao trimestre, 0 nos demais."
    for prefix, meaning in [
        ("val_", "Valor analítico para cálculos, por padrão somente medição exata"),
        ("lim_", "Magnitude padronizada do resultado, interpretada junto ao qualificador; não é limite regulatório"),
        ("qual_", "Operador normalizado que qualifica o resultado numérico"),
        ("unid_", "Unidade padronizada do parâmetro na coleta"),
        ("lag1_", "Valor analítico da coleta imediatamente anterior no ponto"),
        ("delta_", "Valor analítico atual menos o da coleta imediatamente anterior"),
        ("mm4_", "Média móvel de até quatro coletas do ponto, incluindo a atual e ignorando valores nulos"),
    ]:
        if col.startswith(prefix):
            param = col[len(prefix):]
            label = " / ".join(sorted(PARAMETERS.get(param, {param.replace("_", " ")})))
            return meaning + ": " + label + "."
    if col.endswith("_mg_l"):
        alias = "val_" + col[:-5]
        if alias in SCHEMA_TYPES["features_coletas_wide"]:
            THM_ALIASES[col] = alias
            return source_description(alias)
    return ""


def unit_for(col, table):
    if col in {"latitude_decimal", "longitude_decimal", "latitude_ana", "longitude_ana", "latitude_planta_eta",
               "longitude_planta_eta", "latitude_cetesb", "longitude_cetesb"}:
        return "graus decimais; datum não confirmado; ANA arredonda a 6 casas"
    if col in DATE_COLUMNS:
        return "data civil; Excel dd/mm/aaaa ou ISO; saída lógica yyyy-mm-dd"
    if col in {"hora_coleta", "hora_decimal"}:
        return "horário sem fuso" if col == "hora_coleta" else "hora decimal; 4 casas"
    if col in {"altitude_m", "altitude"}:
        return "m (convenção do código)"
    if col.startswith(("qual_", "unid_")):
        return "texto do qualificador" if col.startswith("qual_") else "símbolo da unidade"
    if col in THM_ALIASES:
        return unit_for(THM_ALIASES[col], "features_coletas_wide")
    if col in REL_ALIASES:
        return unit_for(REL_ALIASES[col], table)
    for prefix in ("val_", "lim_", "lag1_", "delta_", "mm4_"):
        if col.startswith(prefix) and col != "delta_temp_ar_agua":
            param = col[len(prefix):]
            if param in UNITS:
                return " | ".join(sorted(UNITS[param])) or "sem unidade declarada"
    if col in {"val_nitrogenio_inorganico_dissolvido", "val_nitrogenio_total_calc", "val_nitrogenio_organico", "val_solido_suspenso_estimado"}:
        return "mg/L"
    if col.endswith("_ug_l"):
        return "µg/L"
    if col.endswith("_mg_l") or col in {"dbo_utilizada_mg_l", "cod_utilizado_mg_l"}:
        return "mg/L"
    if col in {"dbo_quadrado", "od_quadrado", "fosforo_total_quadrado"}:
        return "(mg/L)²"
    if "uv254" in col:
        return "cm⁻²" if "quadrado" in col else "cm⁻¹"
    if col == "lat_x_long":
        return "graus²"
    if col == "razao_sdt_condutividade":
        return "(mg/L)/(µS/cm)"
    if col == "delta_temp_ar_agua":
        return "°C"
    if "perc" in col or col == "od_percentual_saturacao":
        return "%"
    if col.endswith("_l_s"):
        return "L/s"
    if col == "distancia_km":
        return "km; 2 casas"
    if col == "dias_desde_ultima_coleta":
        return "dias"
    if table == "features_resumo_parametros" and col in {"media", "desvio_padrao", "minimo", "p25", "mediana", "p75", "iqr", "maximo", "tendencia_anual_slope"}:
        return "unidade do grupo/ano" if col == "tendencia_anual_slope" else "unidade do grupo; 6 casas"
    if col in {"valor", "valor_numerico"}:
        return "unidade original da linha"
    if col in {"valor_ajustado", "limite_numerico"}:
        return "unidade_padronizada da linha"
    return "adimensional/contagem" if col.endswith("_bin") or col.startswith(("qtd_", "flag_", "razao_", "iet_", "iqa_")) else "não aplicável ou definido no domínio; ver regra"


def column_record(table, col):
    rid = get_rule(table, col)
    rule = RULE_BY_ID[rid]
    _, file, start, end, function, goal, inputs, conditions = rule
    found = assignment(file, col, start, end)
    if table == "estudo_thm_base":
        found = assignment(file, col, 750, 834)
    if table == "predicoes_risco_thm":
        found = assignment(file, col, 850, 917)
    if table == "ana_captacoes_etas_sp":
        found = assignment("integrar_ana_etas.py", col, 130, 161) or found
    expression = ast.unparse(found[2]) if found else ""
    code_start, code_end = (found[0], found[1]) if found else (start, end)
    source = []
    classification = "Transformação"
    nulls = "Ausência da fonte, resultado não interpretável ou insumos insuficientes conforme a regra; ausência não significa zero."
    if col in NULL_COLUMNS:
        classification = "Nulo intencional"
        nulls = "Campo deliberadamente indisponível no contrato atual; modelo, medição ou metodologia não validados. Não interpretar como risco zero."
    if table == "ingestao_excel":
        if col in RAW_COLUMNS:
            source = [f"Excel.{h}" for h in sorted(HEADERS.get(col, {col}))]
            if col in {"periodo_de", "periodo_ate"}:
                source.append("EXT-EXCEL: intervalo total solicitado, quando produzido pelo scraper atual")
        else:
            raw_source = {"valor_numerico": "valor", "altitude_m": "altitude", "latitude_decimal": "latitude",
                          "longitude_decimal": "longitude", "abaixo_limite_deteccao": "sinal"}
            source = [f"ingestao_excel.{raw_source[col]}"] if col in raw_source else ["Nome do arquivo XLSX"]
            if col == "arquivo_origem":
                classification = "Geração técnica"
            clean = assignment("pipeline_cetesb.py", col, 264, 269)
            if clean:
                code_start, code_end, node = clean
                expression = ast.unparse(node)
                file, function = "pipeline_cetesb.py", "clean_raw_frame"
    elif table == "predicoes_pfthm_recalibradas":
        if col in COLLECTION_KEY + ["cod_observado_mg_l", "pfthm_observado_ug_l"]:
            source = ["estudo_thm_base." + col]
            classification = "Cópia direta"
        elif col == "versao_dados_origem":
            source = ["estudo_thm_base.versao_dados"]
            classification = "Cópia direta"
        elif col == "versao_modelo":
            source = ["Data/hora UTC da execução; identidade dos artefatos de recalibração"]
            classification = "Geração técnica"
        else:
            source = ["estudo_thm_base." + c for c in COLLECTION_KEY + ["cod_observado_mg_l", "pfthm_observado_ug_l"]]
        expression = expression or "prepare → split_masks → fit_model/validação → reajuste sem testes → predict/calibrate → main"
        nulls = "Previsões brutas exigem COD/data utilizáveis; selecionada também exige resultado finito não negativo e COD dentro da faixa de ajuste. Medições ausentes não são preenchidas."
    elif table == "exportacao_brutos_excel":
        source = ["ingestao_excel." + col]
        classification = "Cópia direta"
        expression = "df.reindex(columns=headers); ws.append(row)"
        nulls = "Ausências da ingestão exportadas; histórico não reconciliado com a execução atual."
    elif table == "medicoes_tratadas" and col in RAW_COLUMNS + TECH_COLUMNS:
        source = ["ingestao_excel." + col]
        classification = "Cópia direta"
        expression = "raw = pd.concat(raw_parts, ignore_index=True); df = df_all.copy()"
        code_start, code_end = 45, 48
    elif table == "cache_mapa_sp" and col in RECAL_CACHE_COLUMNS:
        source = ["estudo_thm_base." + c for c in ("codigo_ponto", "data_coleta", "cod_observado_mg_l", "pfthm_observado_ug_l")]
        classification = "Transformação"
        nulls = "Medição tem prioridade; previsão nula sem COD/data exatos, fora da faixa de ajuste ou com resultado negativo/não finito."
        expression = "apply_selected_model(frame); normalize_map_cache"
    elif table == "cache_mapa_sp":
        origin = "estudo_thm_base" if col in FRAMES["estudo_thm_base"] else "predicoes_risco_thm"
        source = [origin + "." + col]
        classification = "Transformação" if col == "data_coleta" else "Cópia direta"
        nulls = "Ausências herdadas da tabela de origem; infinitos também viram nulo na normalização física."
        expression = "study.merge(predictions[additional], on=COLLECTION_KEY, validate='one_to_one'); normalize_map_cache"
    elif table == "features_coletas_wide" and col in META_COLUMNS:
        source = ["medicoes_tratadas." + col]
        classification = "Cópia direta"
        expression = "df[meta_cols].drop_duplicates(subset=key_cols)"
        nulls = "Ausência herdada da limpeza inicial; metadados da primeira linha da coleta."
    elif table == "features_coletas_wide" and rid == "WIDE-PIVOT":
        prefix, param = col.split("_", 1)
        mapping = {"val": "valor_ajustado", "lim": "limite_numerico", "qual": "qualificador", "unid": "unidade_padronizada"}
        source = ["medicoes_tratadas." + mapping[prefix], "medicoes_tratadas.param_slug"]
        expression = f"pivot(index=COLLECTION_KEY, columns='param_slug', values='{mapping[prefix]}'); param_slug='{param}'"
        nulls = "Parâmetro não registrado/não numérico na coleta; em val_* também censura não substituída. Lim/qual/unid só existem para parâmetros principais."
    elif table == "features_coletas_wide" and col.startswith(("lag1_", "delta_", "mm4_")) and col != "delta_temp_ar_agua":
        prefix, param = col.split("_", 1)
        source = ["features_coletas_wide.val_" + param, "features_coletas_wide.codigo_ponto",
                  "features_coletas_wide.data_coleta", "features_coletas_wide.hora_coleta"]
        if prefix == "delta":
            source.append("features_coletas_wide.lag1_" + param)
        expression = {"lag1": "groupby(codigo_ponto).shift(1)", "delta": "val - lag1",
                      "mm4": "groupby(codigo_ponto).rolling(window=4, min_periods=1).mean()"}[prefix]
        nulls = "Primeira coleta ou anterior ausente para lag1/delta; janela sem nenhum valor válido para mm4."
    elif table == "features_resumo_parametros":
        source = ["medicoes_tratadas." + c for c in (SUMMARY_KEY if col in SUMMARY_KEY else SUMMARY_KEY + ["valor_ajustado", "qualificador", "data_coleta"])]
        if col in SUMMARY_KEY:
            source = ["medicoes_tratadas." + ("unidade_padronizada" if col == "unidade" else col)]
            classification = "Renomeação" if col == "unidade" else "Cópia direta"
    if table == "predicoes_risco_thm" and col in {
            "modelo_pfthm_ipt", "pfthm_ipt_bruto_ug_l", "pfthm_predito_ipt_ug_l", "status_modelo_pfthm",
            "pfthm_modelo_1_ipt_bruto_ug_l", "pfthm_modelo_2_ipt_bruto_ug_l",
            "pfthm_modelo_1_ipt_ug_l", "pfthm_modelo_2_ipt_ug_l", "aplicacao_modelo_pfthm"}:
        source = ["features_coletas_wide.codigo_ponto"]
        if col not in {"modelo_pfthm_ipt", "aplicacao_modelo_pfthm"}:
            source += ["features_coletas_wide.val_carbono_organico_dissolvido",
                       "features_coletas_wide.qual_carbono_organico_dissolvido",
                       "features_coletas_wide.data_coleta"]
        classification = "Modelo histórico experimental" if col.startswith("pfthm_") else "Proveniência do modelo"
        nulls = "COD ausente/censurado/inválido ou data JFM ausente; estimativa utilizável também rejeita resultado negativo/não finito. Identificadores de modelo/aplicação independem da disponibilidade do COD."
    if not source and found:
        for var, c in sorted(refs(found[2], file, found[0])):
            if var == "ana_row":
                source.append("ana_captacoes_etas_sp." + c)
            elif var == "st":
                source.append("cache_mapa_sp." + c)
            elif var in {"predicoes_df", "models"}:
                source.append("predicoes_risco_thm." + c)
            elif var in {"w", "base", "df_wide"}:
                source.append("features_coletas_wide." + c)
            else:
                origin = "ingestao_excel" if table == "medicoes_tratadas" and c in RAW_COLUMNS + TECH_COLUMNS else "medicoes_tratadas"
                source.append(origin + "." + c)
        if isinstance(found[2], ast.Subscript) and isinstance(found[2].value, ast.Name) and found[2].value.id in {"w", "ana_row", "st"}:
            classification = "Cópia direta" if literal(found[2].slice) == col else "Renomeação"
        if isinstance(found[2], ast.Constant) or expression == "DATA_VERSION":
            classification = "Nulo intencional" if col in NULL_COLUMNS else "Geração técnica"
    if table == "features_coletas_wide" and col in {"qtd_parametros_medidos", "qtd_parametros_censurados", "perc_parametros_censurados"}:
        source = ["medicoes_tratadas.parametro", "medicoes_tratadas.abaixo_limite_deteccao"]
    if table == "medicoes_tratadas" and col in {"unidade_padronizada", "limite_numerico"}:
        source = ["ingestao_excel.unidade", "medicoes_tratadas.param_slug"]
        if col == "limite_numerico":
            source.append("ingestao_excel.valor_numerico")
    if table == "features_coletas_wide" and rid == "WIDE-CATEGORIA":
        source = ["medicoes_tratadas.valor", "medicoes_tratadas.param_slug"]
    if table == "features_coletas_wide" and rid == "WIDE-CONAMA":
        param = {"violacao_od": "oxigenio_dissolvido", "violacao_dbo": "dbo_5_20",
                 "violacao_turbidez": "turbidez", "violacao_fosforo": "fosforo_total",
                 "violacao_amonia": "nitrogenio_amoniacal", "violacao_ph": "ph"}.get(col)
        dependencies = ["classe"]
        if param:
            dependencies += ["lim_" + param, "qual_" + param]
        if col == "violacao_fosforo":
            dependencies += ["tipo_de_sistema_hidrico"]
        if col == "violacao_amonia":
            dependencies += ["val_ph", "qual_ph"]
        if col in {"qtd_parametros_conama_avaliados", "qtd_violacoes_conama"}:
            dependencies = ["violacao_od", "violacao_dbo", "violacao_turbidez", "violacao_fosforo", "violacao_amonia", "violacao_ph"]
        if col == "perc_violacoes_conama":
            dependencies = ["qtd_violacoes_conama", "qtd_parametros_conama_avaliados"]
        if col == "status_conama":
            dependencies = ["qtd_parametros_conama_avaliados"]
        source = ["features_coletas_wide." + c for c in dependencies]
        if col == "violacao_ecoli":
            source = ["Decisão metodológica: não substituir coliformes por E. coli automaticamente"]
    if table == "features_coletas_wide" and col == "iqa_experimental":
        source = ["features_coletas_wide." + c for c in ["od_percentual_saturacao", "val_escherichia_coli", "val_ph",
                  "val_dbo_5_20", "val_fosforo_total", "val_nitrogenio_total_calc", "val_turbidez", "val_solido_total"]]
    if table == "ana_captacoes_etas_sp":
        source = ["ANA ArcGIS atributos/geometry ou JSON anterior; expressão: " + expression]
    if table in {"estudo_thm_base", "predicoes_risco_thm"}:
        for item in list(source):
            if item.startswith("features_coletas_wide.val_"):
                qualifier = item.replace(".val_", ".qual_", 1)
                if qualifier.split(".", 1)[1] in SCHEMA_TYPES["features_coletas_wide"]:
                    source.append(qualifier)
        if col in {"iqa_estimado", "classificacao_iqa"}:
            classification = "Transformação" if ".round(" in expression else "Cópia direta"
            nulls = "Nulo herdado da wide, onde a metodologia oficial é deliberadamente indisponível."
    if not source:
        source = ["Constante/decisão metodológica no código" if classification in {"Geração técnica", "Nulo intencional"} else inputs]
    source = sorted(set(s for s in source if s != table + "." + col))
    desc = source_description(col)
    status = "Descrito pelo código atual; domínio requer validação externa"
    if table == "exportacao_brutos_excel":
        status = "Exportação histórica: regra atual de cópia descrita, conteúdo não reconciliado"
    if not desc:
        desc = "Significado não confirmado; não inferido apenas pelo nome. Consultar origem, código e pendência."
        status = "Pendente: significado funcional"
        issue("COLUNA-" + table + "-" + col, table + "." + col, desc, "Confirmar definição funcional na fonte.")
    info = PROFILES.get(table, {}).get(col, {})
    sample = info.get("exemplo", "Não perfilado")
    observed = sorted(info.get("dominio", []))
    domain = ("Amostra de valores observados (não exaustiva): " if info.get("dominio_limitado") else "Valores distintos observados (não domínio oficial): ") + " | ".join(observed[:32])
    if not observed:
        domain = "Nenhum valor não nulo observado" if info else "Não perfilado"
    if col.endswith("_bin") or col in {"abaixo_limite_deteccao", "captacao_bin"}:
        domain += "; significado 0/1 descrito acima e na regra"
    physical = " | ".join(sorted(SCHEMA_TYPES[table].get(col, set())))
    persistence = "Sem conversão física adicional documentada nesta ocorrência."
    rule_ids = [rid]
    if table in ANALYTIC + ["medicoes_tratadas", "cache_mapa_sp"]:
        rule_ids.append("PERSIST-PARQUET")
        persistence = "write_parquet converte object em string e omite índice; tipos efetivamente observados listados por esquema/objeto."
    if table == "cache_mapa_sp":
        rule_ids.append("CACHE-TIPOS")
        persistence += " Antes da gravação: data yyyy-mm-dd; inteiros int64, reais float64 e demais string."
    if table == "ingestao_excel":
        physical = "Tipos das mesmas colunas nas partições tratadas (não tipos das células Excel): " + physical
        persistence = "Na ingestão SQL usa sql_frame; tipo SQL efetivo deve ser conferido por tabela, pois as cargas antigas diferem do contrato atual."
    if col in DATE_COLUMNS:
        logical = "data"
    elif any(x in physical for x in ("double", "float")):
        logical = "número real"
    elif any(x in physical for x in ("int", "bool")):
        logical = "booleano" if "bool" in physical else "inteiro"
    else:
        logical = "texto"
    key = PURPOSES[table][1]
    return {
        "tabela": table, "coluna": col, "descricao": desc, "tipo_logico": logical,
        "tipo_local_observado": physical, "tipo_sql_observado": "Não consultado para esta tabela lógica",
        "unidade_formato": unit_for(col, table), "granularidade": PURPOSES[table][0],
        "papel_chave": "Componente de chave lógica" if col in [x.strip() for x in key.split(",")] else "Atributo; chave da tabela: " + key,
        "dominio": domain, "nulos_regra": nulls,
        "nulos_observados": f"{info.get('nulos', '?')}/{info.get('total', '?')} (somente artefato local)",
        "origem_imediata": " | ".join(source), "classificacao": classification,
        "regra": " | ".join(rule_ids), "expressao": expression or conditions, "persistencia": persistence,
        "codigo": f"{file}:{code_start}-{code_end}", "funcao": function, "codigo_sha256": HASHES[file],
        "exemplo": sample, "local_exemplo": info.get("local_exemplo", "Não perfilado"),
        "status": status, "_sources": source,
    }


def sql_type(c):
    t = c["tipo"]
    if t in {"nvarchar", "nchar"}:
        size = "MAX" if c["comprimento_bytes"] == -1 else c["comprimento_bytes"] // 2
        t += f"({size})"
    elif t in {"varchar", "char", "varbinary", "binary"}:
        t += f"({'MAX' if c['comprimento_bytes'] == -1 else c['comprimento_bytes']})"
    elif t in {"decimal", "numeric"}:
        t += f"({c['precisao']},{c['escala']})"
    return t + (" NULL" if c["aceita_nulo"] else " NOT NULL")


def trace_origins(table, col, lookup, visited=None):
    visited = set() if visited is None else visited
    key = table + "." + col
    if key in visited:
        return set()
    visited = visited | {key}
    row = lookup.get((table, col))
    if not row:
        return {key}
    origins = set()
    for source in row["_sources"]:
        parts = source.split(".", 1)
        if len(parts) == 2 and tuple(parts) in lookup:
            origins |= trace_origins(*parts, lookup, visited)
        else:
            origins.add(source)
    return origins


def trace_rules(table, col, lookup, visited=None):
    visited = set() if visited is None else visited
    key = (table, col)
    if key in visited or key not in lookup:
        return []
    row = lookup[key]
    stages = []
    for source in row["_sources"]:
        parent = tuple(source.split(".", 1))
        if parent in lookup:
            stages.extend(trace_rules(*parent, lookup, visited | {key}))
    stages.extend(row["regra"].split(" | "))
    return list(dict.fromkeys(stages))


def build_catalog(sql):
    columns = []
    for table in PURPOSES:
        for col in SCHEMA_TYPES[table]:
            columns.append(column_record(table, col))
    for row in columns:
        if row["tabela"] == "predicoes_risco_thm" and row["coluna"] in {
                "modelo_pfthm_ipt", "pfthm_ipt_bruto_ug_l", "pfthm_predito_ipt_ug_l", "status_modelo_pfthm",
                "pfthm_modelo_1_ipt_bruto_ug_l", "pfthm_modelo_2_ipt_bruto_ug_l",
                "pfthm_modelo_1_ipt_ug_l", "pfthm_modelo_2_ipt_ug_l", "aplicacao_modelo_pfthm"}:
            row["regra"] += " | THM-IPT-COD"
    lookup = {(r["tabela"], r["coluna"]): r for r in columns}
    for row in columns:
        row["fonte_original"] = " | ".join(sorted(trace_origins(row["tabela"], row["coluna"], lookup)))
        row["regras_desde_origem"] = " → ".join(trace_rules(row["tabela"], row["coluna"], lookup))
    tables = []
    for name, (grain, key, flow) in PURPOSES.items():
        schema = SCHEMA_TYPES[name]
        p = PROFILES.get(name, {})
        rows = next(iter(p.values()))["total"] if p else None
        category = {"ingestao_excel": "Contrato compartilhado de ingestão", "exportacao_brutos_excel": "Exportação histórica",
                    "pontos_monitoramento": "Catálogo auxiliar"}.get(name, "Modelo lógico")
        tables.append({"tabela": name, "classe": category,
                       "finalidade_granularidade": grain, "chave_logica": key, "fluxo": flow,
                       "linhas": rows, "colunas": len(schema), "chave_fisica": "Não presume PK/UNIQUE no banco",
                       "origem_contagem": "Perfil integral do artefato atual; exportação histórica sem contagem integral",
                       "funcoes_responsaveis": " | ".join(sorted({r["funcao"] for r in columns if r["tabela"] == name})),
                       "objetos": " | ".join(x["objeto"] for x in PHYSICAL if x["tabela_logica"] == name and "Backup" not in x["classe"])})
    sql_tables = defaultdict(list)
    for c in sql.get("colunas", []):
        sql_tables[(c["esquema"], c["tabela"])].append(c)
    for (schema, name), fields in sql_tables.items():
        full = schema + "." + name
        raw = set(RAW_COLUMNS + TECH_COLUMNS).issubset({c["coluna"] for c in fields})
        model = "ingestao_excel" if raw else name
        backup = schema != "dbo"
        definition = PURPOSES.get(model, ("Objeto SQL sem classificação confirmada", "Chave lógica não confirmada", "Origem pendente"))
        tables.append({"tabela": full, "classe": "Backup SQL" if backup else ("Tabela SQL de ingestão" if raw else "Tabela SQL atual"),
                       "finalidade_granularidade": definition[0], "chave_logica": definition[1], "fluxo": definition[2],
                       "linhas": fields[0]["linhas_metadados"], "colunas": len(fields),
                       "chave_fisica": "Nenhum índice único/PK observado" if not sql.get("chaves") else "Ver sql_inventario.json",
                       "origem_contagem": "sys.partitions; não COUNT_BIG",
                       "funcoes_responsaveis": "Histórico não confirmado" if backup else
                       ("sanitize_table_name; load_and_clean_raw_file; clean_raw_frame; upload_dataframe_to_sql; sql_frame" if raw else
                        "recalibrar_pfthm.calibrate; recalibrar_pfthm.publish; sql_frame" if model == "predicoes_pfthm_recalibradas" else
                        "load_tables; sql_frame; publicador SQL; funções analíticas vinculadas nas colunas"),
                       "objetos": full})
        expected = set(SCHEMA_TYPES.get(model, {}))
        actual = {c["coluna"] for c in fields}
        if not raw and not backup and expected != actual:
            issue("SQL-ESQUEMA-" + full, full, f"Somente SQL: {sorted(actual-expected)}; somente local: {sorted(expected-actual)}", "Reconciliar versões.", "Alta")
        if backup:
            issue("BACKUP-" + full, full, f"Esquema histórico com {len(fields)} colunas; só histórico: {sorted(actual-expected)}; ausentes frente ao atual: {sorted(expected-actual)}",
                  "Código histórico não disponível; entradas inventariadas, sem atribuição retroativa de regras atuais.")
        for c in fields:
            col = c["coluna"]
            base = lookup.get((model, col))
            row = dict(base) if base else {
                "coluna": col, "descricao": "Coluna histórica ou desconhecida; significado e regra original não confirmados.",
                "tipo_logico": "não confirmado", "tipo_local_observado": "", "unidade_formato": "não confirmada",
                "granularidade": definition[0], "papel_chave": definition[1], "dominio": "Não consultado",
                "nulos_regra": "Não confirmada", "nulos_observados": "Não consultado", "origem_imediata": "Histórico indisponível",
                "classificacao": "Não confirmada", "regra": "", "expressao": "", "codigo": "", "funcao": "", "codigo_sha256": "",
                "exemplo": "Não consultado", "local_exemplo": "", "status": "Pendente: documentação histórica",
                "fonte_original": "Histórico indisponível", "regras_desde_origem": "Não confirmado", "_sources": [],
            }
            row["tabela"] = full
            row["tipo_sql_observado"] = sql_type(c)
            if backup:
                row.update({"status": "Pendente: código/proveniência históricos", "classificacao": "Não confirmada (histórico)",
                            "descricao": "HISTÓRICO — definição atual apenas para comparação, não comprovada para este backup: " + row["descricao"],
                            "regra": "", "codigo": "", "funcao": "", "codigo_sha256": "", "expressao": "",
                            "origem_imediata": "Publicação anterior preservada; transformação histórica não confirmada",
                            "regras_desde_origem": "Não confirmado para o backup",
                            "fonte_original": "Não confirmada para o backup", "exemplo": "Não consultado",
                            "local_exemplo": "Nenhum valor do backup SQL foi lido", "nulos_observados": "Não consultado",
                            "dominio": "Não consultado", "nulos_regra": "Não confirmada para o backup", "_sources": []})
            elif raw:
                local = EXCEL_FILES.get(name)
                if local:
                    row["fonte_original"] = f"{local['arquivo']} / aba {local['aba']} / " + row["fonte_original"]
                    code = Path(local["arquivo"]).name.split(" - ")[0]
                    p = RAW_PROFILES.get(code, {}).get(col) if local["atual"] else None
                    row["exemplo"] = p["exemplo"] if p else "Não perfilado"
                    row["local_exemplo"] = p["local_exemplo"] if p else "Arquivo histórico inventariado, valores não reconciliados"
                    row["nulos_observados"] = f"{p['nulos']}/{p['total']} (Parquet local, não SQL)" if p else "Não consultado"
                    if p:
                        prefix = "Amostra não exaustiva" if p["dominio_limitado"] else "Valores distintos"
                        row["dominio"] = prefix + " observados no Parquet deste ponto, não no SQL: " + " | ".join(sorted(p["dominio"]))
                        if not p["dominio"]:
                            row["dominio"] = "Nenhum valor não nulo observado no Parquet deste ponto; SQL não perfilado"
                    else:
                        row["dominio"] = "Não perfilado para este arquivo histórico; não reutilizar domínio de outras fontes como se fosse deste objeto."
                row["status"] = "Regra atual documentada; conteúdo e histórico da ingestão SQL não reconciliados"
                row["origem_imediata"] += " | publicação: pipeline_cetesb.py:1095-1099 → sql_frame"
                row["regra"] += " | ING-NOME | PERSIST-RAW-SQL | PERSIST-SQL"
                row["regras_desde_origem"] += " → ING-NOME → PERSIST-RAW-SQL → PERSIST-SQL"
            else:
                publication = "RECAL-PUBLICACAO" if model == "predicoes_pfthm_recalibradas" else "PERSIST-PUBLICACAO"
                function = "recalibrar_pfthm.publish" if model == "predicoes_pfthm_recalibradas" else "load_tables"
                row["origem_imediata"] = model + "." + col + " | " + function + " → sql_frame"
                row["regra"] += " | " + publication + " | PERSIST-SQL"
                row["regras_desde_origem"] += " → " + publication + " → PERSIST-SQL"
                row["status"] = "Esquema SQL confirmado; valores do exemplo são locais, não consultados no SQL"
                if base:
                    base["tipo_sql_observado"] = sql_type(c)
            columns.append(row)
        fields_schema = [{"coluna": c["coluna"], "tipo": sql_type(c), "aceita_nulo": bool(c["aceita_nulo"])} for c in fields]
        PHYSICAL.append({"objeto": full, "classe": "Backup SQL" if backup else "Tabela SQL",
                         "tabela_logica": model, "linhas": fields[0]["linhas_metadados"], "quantidade_colunas": len(fields),
                         "esquema_id": schema_id(fields_schema), "bytes": None, "modificado_utc": sql["consultado_em_utc"],
                         "sha256": "Não aplicável; metadados da consulta em sql_inventario.json"})
    return tables, columns


def example_inputs(sample):
    table, col = sample["tabela"], sample["coluna"]
    position = re.search(r"linha de dados (\d+)", sample["local_exemplo"])
    if not position:
        return "Sem linha de exemplo não nula; ver trecho e testes."
    index = int(position.group(1)) - 1
    frame = FRAMES.get(table)
    origin = sample["local_exemplo"].split(";", 1)[0]
    if frame is None and origin.endswith(".parquet"):
        frame = pd.read_parquet(ROOT / origin)
    if frame is None or index >= len(frame):
        return "Entrada indicada na origem e no código; sem perfil tabular para este exemplo."
    current = frame.iloc[index]
    values = {}
    for source in sample["_sources"]:
        parts = source.split(".", 1)
        if len(parts) != 2:
            continue
        source_table, source_col = parts
        source_frame = FRAMES.get(source_table)
        if source_table in {"ingestao_excel", "medicoes_tratadas"}:
            if table in {"ingestao_excel", "medicoes_tratadas"} and source_col in frame:
                values[source] = text_value(current[source_col])
                continue
            if "codigo_ponto" in current:
                source_path = ROOT / "resultados_v2/medicoes_tratadas" / f"{current['codigo_ponto']}.parquet"
                if source_path.exists():
                    source_frame = pd.read_parquet(source_path)
        if source_frame is None or source_col not in source_frame:
            continue
        subset = source_frame
        keys = SUMMARY_KEY if table == "features_resumo_parametros" else COLLECTION_KEY
        for key in keys:
            if key in current and key in subset:
                subset = subset.loc[subset[key].map(text_value).eq(text_value(current[key]))]
        if table == "features_coletas_wide" and sample["regra"].startswith("WIDE-PIVOT") and "param_slug" in subset:
            subset = subset.loc[subset.param_slug.eq(col.split("_", 1)[1])]
        if len(subset) == 1:
            values[source] = text_value(subset.iloc[0][source_col])
        elif len(subset):
            values[source] = {"linhas_origem": len(subset), "primeiros_valores": subset[source_col].head(5).map(text_value).tolist()}
    return json.dumps(values, ensure_ascii=False) if values else "Constante ou entrada externa: " + sample["origem_imediata"]


def build_rules(columns):
    result = []
    for rid, file, start, end, function, goal, inputs, conditions in RULES:
        affected = [r for r in columns if rid in r["regra"].split(" | ") and "." not in r["tabela"]]
        sample = next((r for r in affected if r["exemplo"] not in {"NULL", "Não perfilado"}), None)
        result.append({"id": rid, "objetivo": goal, "entradas": inputs,
                       "saidas": " | ".join(r["tabela"] + "." + r["coluna"] for r in affected) or "Controle de linhas, persistência ou etapa auxiliar; ver código",
                       "condicoes_nulos": conditions, "codigo": f"{file}:{start}-{end}", "funcao": function,
                       "codigo_sha256": HASHES[file], "trecho_real": "\n".join(f"{i}: {line}" for i, line in enumerate(SOURCES[file].splitlines(), 1) if start <= i <= end),
                       "exemplo_entrada_observada": example_inputs(sample) if sample else "Etapa de controle/infraestrutura ou saída nula: ver testes e código, sem amostra pareada disponível.",
                       "exemplo_saida_observada": (sample["tabela"] + "." + sample["coluna"] + "=" + sample["exemplo"] + "; " + sample["local_exemplo"]) if sample else "Ver testes/reconciliações; nenhuma entrada/saída numérica amostrada para esta regra",
                       "validacao": "test_validacao.py e reconciliacoes.json; cobertura específica descrita nas funções de teste, não validação científica"})
    return result


def reconcile():
    from consolidar_brutos_excel import fast_read_cetesb_xlsx
    from pipeline_cetesb import load_and_clean_raw_file, prepare_measurements, build_features_coletas_wide, build_estudo_thm_tables, build_features_resumo_parametros, build_map_cache
    results = []
    for code in ["BILL02030", "ABAN02790", "ANOR02300", "ARPE02800"]:
        path = ROOT / f"{code} - 2010-2026.xlsx"
        standard = load_and_clean_raw_file(str(path))
        fast = fast_read_cetesb_xlsx(str(path))
        pd.testing.assert_frame_equal(standard, fast)
        treated = prepare_measurements(standard)
        stored = pd.read_parquet(ROOT / "resultados_v2/medicoes_tratadas" / f"{code}.parquet")
        for col in treated:
            a = treated[col].map(text_value).reset_index(drop=True)
            b = stored[col].map(text_value).reset_index(drop=True)
            pd.testing.assert_series_equal(a, b, check_names=False)
        wide, study, pred = build_estudo_thm_tables(build_features_coletas_wide(standard))
        summary = build_features_resumo_parametros(standard)
        frames = {"features_coletas_wide": wide, "estudo_thm_base": study, "predicoes_risco_thm": pred,
                  "features_resumo_parametros": summary}
        for name, actual in frames.items():
            expected = FRAMES[name].loc[FRAMES[name].codigo_ponto.eq(code)].reset_index(drop=True)
            if actual.empty:
                assert expected.empty, (code, name)
                results.append({"teste": f"{code}: Excel → tratado → {name}", "status": "PASSOU",
                                "linhas": 0, "colunas": len(expected.columns),
                                "observacao": "Nenhum resultado numérico para resumo; ausência de linhas reconciliada."})
                continue
            keys = SUMMARY_KEY if name == "features_resumo_parametros" else COLLECTION_KEY
            actual = actual.sort_values(keys).reset_index(drop=True)
            expected = expected.sort_values(keys).reset_index(drop=True)
            for col in actual:
                if pd.api.types.is_numeric_dtype(actual[col]):
                    np.testing.assert_allclose(actual[col].to_numpy(dtype=float, na_value=np.nan),
                                               pd.to_numeric(expected[col]).to_numpy(dtype=float, na_value=np.nan),
                                               rtol=0, atol=1e-12, equal_nan=True)
                else:
                    pd.testing.assert_series_equal(actual[col].map(text_value), expected[col].map(text_value), check_names=False)
            results.append({"teste": f"{code}: Excel → tratado → {name}", "status": "PASSOU", "linhas": len(actual),
                            "colunas": len(actual.columns), "tolerancia_absoluta_numerica": 1e-12,
                            "observacao": "Todas as colunas produzidas para o ponto; colunas de outros pontos podem existir somente na união global."})
        numeric = treated.loc[treated.valor_numerico.notna()].head(1)
        for _, r in numeric.iterrows():
            results.append({"teste": f"Exemplo rastreado {code}", "status": "PASSOU",
                            "entrada": {"arquivo": path.name, "parametro": r.parametro, "valor": r.valor, "sinal": text_value(r.sinal), "unidade": text_value(r.unidade)},
                            "saida": {"param_slug": r.param_slug, "limite_numerico": text_value(r.limite_numerico),
                                      "valor_ajustado": text_value(r.valor_ajustado), "unidade_padronizada": text_value(r.unidade_padronizada)}})
    cache = build_map_cache(FRAMES["estudo_thm_base"], FRAMES["predicoes_risco_thm"])
    pd.testing.assert_frame_equal(cache, FRAMES["cache_mapa_sp"], check_exact=True)
    cache_paths = [ROOT / "cache_mapa_sp.parquet"]
    optional_cache = ROOT / "resultados_v2/cache_publicacao.parquet"
    if optional_cache.exists():
        cache_paths.append(optional_cache)
    for path in cache_paths:
        pd.testing.assert_frame_equal(cache, pd.read_parquet(path), check_exact=True)
    results.append({"teste": "Junção completa e caches atuais", "status": "PASSOU", "linhas": len(cache),
                    "colunas": len(cache.columns), "comparacao": "exata",
                    "arquivos_adicionais": [p.relative_to(ROOT).as_posix() for p in cache_paths]})
    for name in ["features_coletas_wide", "estudo_thm_base", "predicoes_risco_thm"]:
        assert not FRAMES[name].duplicated(COLLECTION_KEY).any(), name
        for col in set(FRAMES[name]) & NULL_COLUMNS:
            assert FRAMES[name][col].isna().all(), (name, col)
    rel = FRAMES["relacao_cetesb_ana_etas"]
    assert rel.distancia_km.le(15).all() and rel.vinculo_confirmado.eq(False).all()
    assert not rel.codigo_ponto.duplicated().any()
    results.append({"teste": "Chaves de coletas, nulos intencionais e candidatas ANA", "status": "PASSOU"})
    (OUT / "reconciliacoes.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


def safe_string(value):
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def workbook(sheets):
    book = openpyxl.Workbook(write_only=True)
    for name, records in sheets.items():
        ws = book.create_sheet(name)
        ws.freeze_panes = "C2"
        if not records:
            continue
        keys = [k for k in records[0] if not k.startswith("_")]
        header = []
        for key in keys:
            cell = WriteOnlyCell(ws, value=key)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1D4D60")
            header.append(cell)
        ws.append(header)
        for row in records:
            cells = []
            for key in keys:
                value = safe_string(row.get(key, ""))
                if len(value) > 32767:
                    value = value[:32600] + "\n[Campo excede limite de célula Excel; conteúdo integral em catalogo.json / HTML.]"
                cell = WriteOnlyCell(ws, value=value)
                cell.data_type = "s"
                cells.append(cell)
            ws.append(cells)
        ws.auto_filter.ref = f"A1:{openpyxl.utils.get_column_letter(len(keys))}{len(records)+1}"
        for i, key in enumerate(keys, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = 42 if key not in {"descricao", "fonte_original", "condicoes_nulos"} else 80
    book.save(OUT / "dicionario_pipeline.xlsx")


STYLE = """body{font:16px system-ui;max-width:1400px;margin:30px auto;padding:0 20px;color:#173443;line-height:1.5}
a{color:#005b8e}nav{position:sticky;top:0;background:white;padding:12px;border-bottom:2px solid #ddd;z-index:1}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #ccd7da;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{background:#eaf3f5}code,pre{background:#f0f3f4}pre{padding:12px;white-space:pre-wrap;font-size:13px}
details{border:1px solid #cbdadd;padding:10px;margin:10px 0}summary{cursor:pointer;font-weight:600}
input{padding:10px;width:90%;margin:15px 0}.notice{padding:16px;background:#fff3d9;border-left:5px solid #a77900}
.metric{display:inline-block;padding:16px;margin:4px;background:#edf5f8}dt{font-weight:bold}dd{margin-bottom:9px;overflow-wrap:anywhere}
"""


def page(title, body):
    return "<!doctype html><html lang='pt-BR'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" + \
        "<title>" + html.escape(title) + "</title><style>" + STYLE + "</style><nav><a href='index.html'>Visão geral</a> · " + \
        "<a href='tabelas.html'>Tabelas e colunas</a> · <a href='transformacoes.html'>Transformações</a> · " + \
        "<a href='pendencias.html'>Pendências</a> · <a href='dicionario_pipeline.xlsx'>Planilha</a></nav><h1>" + html.escape(title) + "</h1>" + body + "</html>"


def html_table(rows, fields):
    return "<table><thead><tr>" + "".join("<th>" + html.escape(k) + "</th>" for k in fields) + "</tr></thead><tbody>" + \
        "".join("<tr>" + "".join("<td>" + html.escape(safe_string(r.get(k, ""))) + "</td>" for k in fields) + "</tr>" for r in rows) + "</tbody></table>"


def render(tables, columns, rules, coverage, reconciliation):
    groups = defaultdict(list)
    for row in columns:
        groups[row["tabela"]].append(row)
    links = {name: "tabela_" + hashlib.sha256(name.encode()).hexdigest()[:16] + ".html" for name in groups}
    for name, rows in groups.items():
        filename = links[name]
        body = "<p>Uma entrada individual por coluna. Exemplos são locais; não representam leitura de valores SQL.</p><input id='filter' placeholder='Filtrar coluna, significado ou origem'>"
        for r in rows:
            body += "<details class='coluna' id='" + html.escape(r["coluna"], quote=True) + "'><summary>" + html.escape(r["coluna"] + " — " + r["descricao"]) + "</summary><dl>"
            for k, value in r.items():
                if k.startswith("_"):
                    continue
                if k == "regra":
                    value_html = " · ".join("<a href='transformacoes.html#" + html.escape(x) + "'>" + html.escape(x) + "</a>" for x in value.split(" | ") if x)
                elif k == "origem_imediata":
                    parts = []
                    for source in value.split(" | "):
                        parent, _, column = source.partition(".")
                        if parent in groups and any(item["coluna"] == column for item in groups[parent]):
                            parts.append("<a href='" + links[parent] + "#" + html.escape(column, quote=True) + "'>" + html.escape(source) + "</a>")
                        else:
                            parts.append(html.escape(source))
                    value_html = " | ".join(parts)
                else:
                    value_html = html.escape(safe_string(value))
                body += "<dt>" + html.escape(k) + "</dt><dd>" + value_html + "</dd>"
            body += "</dl></details>"
        body += "<script>document.getElementById('filter').addEventListener('input',e=>{const q=e.target.value.toLocaleLowerCase('pt-BR');document.querySelectorAll('.coluna').forEach(x=>x.hidden=!x.textContent.toLocaleLowerCase('pt-BR').includes(q));});</script>"
        (OUT / filename).write_text(page(name, body), encoding="utf-8")
    body = "<p>Tabelas lógicas, tabelas SQL efetivamente observadas e backups separados. O contrato ingestao_excel é compartilhado, mas cada tabela SQL possui suas próprias entradas.</p>"
    body += "<input id='filter' placeholder='Filtrar tabela ou finalidade'><table><tr><th>Tabela</th><th>Classe</th><th>Linhas</th><th>Colunas</th><th>Granularidade e finalidade</th></tr>"
    for t in tables:
        body += "<tr class='item'><td><a href='" + links[t["tabela"]] + "'>" + html.escape(t["tabela"]) + "</a></td>" + "".join("<td>" + html.escape(safe_string(t[k])) + "</td>" for k in ["classe", "linhas", "colunas", "finalidade_granularidade"]) + "</tr>"
    body += "</table><script>document.getElementById('filter').addEventListener('input',e=>document.querySelectorAll('.item').forEach(x=>x.hidden=!x.textContent.toLowerCase().includes(e.target.value.toLowerCase())));</script>"
    (OUT / "tabelas.html").write_text(page("Catálogo de tabelas e colunas", body), encoding="utf-8")
    body = "<p>Trechos reais do código atual, congelados com SHA-256. Referências não comprovam que backups foram produzidos por esta versão.</p>"
    for r in rules:
        body += "<section id='" + r["id"] + "'><h2>" + r["id"] + "</h2><dl>"
        for k, v in r.items():
            body += "<dt>" + html.escape(k) + "</dt><dd>" + ("<pre>" + html.escape(v) + "</pre>" if k == "trecho_real" else html.escape(v)) + "</dd>"
        body += "</dl></section>"
    (OUT / "transformacoes.html").write_text(page("Catálogo de transformações", body), encoding="utf-8")
    (OUT / "pendencias.html").write_text(page("Cobertura, divergências e pendências",
        "<p>Inventário completo não significa validação semântica ou histórica completa. As pendências abaixo não foram ocultadas para atingir artificialmente 100%.</p>" +
        html_table(ISSUES, ["id", "escopo", "gravidade", "constatacao", "acao", "status"])), encoding="utf-8")
    body = "<p>Documentação auditável da pipeline CETESB/ANA. Gerada em " + coverage["gerado_em_utc"] + ", contrato " + DATA_VERSION + ".</p>"
    body += "<p class='notice'>Não foi executada publicação, download ou reprocessamento de produção. A leitura SQL limitou-se a SELECT de metadados. Probabilidades THM e IQA oficial indisponíveis não são zero. Relações ANA são candidatas, não vínculos comprovados.</p>"
    body += "".join("<div class='metric'><b>" + str(coverage[k]) + "</b><br>" + html.escape(k) + "</div>" for k in ["tabelas_sql", "objetos_fisicos", "entradas_colunas", "regras", "pendencias"])
    body += "<h2>Fluxo de dados</h2><pre>InfoÁguas → catálogo de pontos → janelas XLSX → validação / concatenação / deduplicação\n" + \
        "  ├─ Excel → clean_raw_frame → tabela SQL por arquivo (ingestão, sem prepare_measurements)\n" + \
        "  └─ Excel → leitor rápido → clean_raw_frame → agrupamento por ponto\n" + \
        "       ├─ prepare_measurements → medicoes_tratadas/[ponto].parquet\n" + \
        "       ├─ pivô + variáveis/índices/séries → wide → enriquecimento THM\n" + \
        "       │                                      ├─ estudo_thm_base\n" + \
        "       │                                      └─ predicoes_risco_thm\n" + \
        "       └─ estatísticas por grupo → features_resumo_parametros\n" + \
        "Base THM + predições → inner join 1:1 → quadrático recalibrado congelado → cache local / SQL → mapa\n" + \
        "ANA API ou JSON anterior → cadastro captações/ETAs + último ponto CETESB com coordenadas\n" + \
        "                       → candidatas até 15 km → JSON / duas tabelas SQL\n" + \
        "Parquets analíticos + JSON ANA → publicar_resultados_sql → seis tabelas SQL\n" + \
        "Ingestão local ou SQL → consolidado Excel histórico (exportação, não modelo novo)</pre>"
    body += "<h2>Como usar</h2><ul><li><a href='tabelas.html'>Catálogo navegável</a>: abra uma tabela e filtre as colunas.</li>" + \
        "<li><a href='dicionario_pipeline.xlsx'>Planilha</a>: Tabelas, Colunas, Transformações, Rastreabilidade, Pendências, Objetos, Esquemas e Validações.</li>" + \
        "<li><a href='catalogo.json'>Catálogo completo em JSON</a>, <a href='cobertura.json'>cobertura</a>, <a href='sql_inventario.json'>consulta SQL</a> e <a href='reconciliacoes.json'>reconciliações</a>.</li></ul>"
    body += "<h2>Relações e chaves</h2><p>Coleta = ponto + data + hora, incluindo hora ausente. Wide/base/predições/cache têm chave lógica 1:1. Resumo acrescenta dimensões de cadastro e parâmetro; não é 1:1 com coleta. Uma candidata ANA por ponto pode referenciar a mesma captação que outros pontos. Nenhum índice único/PK foi encontrado na consulta SQL; não confundir a unicidade lógica validada com restrição física.</p>"
    body += "<h2>Limites da cobertura</h2><p>São documentadas individualmente as colunas dinâmicas atuais e as 664 tabelas de ingestão SQL. Esquemas de todos os Parquets, abas Excel e backups locais constam em Objetos/Esquemas. Backups SQL têm entradas individuais, mas regras históricas não disponíveis permanecem pendentes. Exemplos e domínios provêm de artefatos locais; a consulta atual ao SQL não verificou seus valores. A correspondência do arquivo antigo com o código atual não é presumida.</p>"
    body += "<h2>Documentação metodológica reaproveitada</h2><ul><li><a href='../METODOLOGIA_E_VALIDACAO.md'>Metodologia e limites científicos</a>: regras atuais conferidas; não substitui revisão científica.</li><li><a href='../EQUIVALENCIA_PIPELINE.md'>Equivalência anterior</a>: evidência histórica, não nova consulta integral nesta execução.</li><li><a href='../RESULTADOS_VALIDACAO.md'>Resultados de 16/11</a>: distingue estado antes e depois da publicação.</li></ul>"
    body += "<h2>Verificação nesta execução</h2>" + html_table(reconciliation, ["teste", "status", "linhas", "colunas"])
    body += "<h2>Reproduzir sem alterar a pipeline</h2><pre>python Doc/pipeline/inventariar_sql.py\npython Doc/pipeline/gerar_documentacao.py\npython Doc/pipeline/validar_documentacao.py\npython -m unittest test_validacao -v</pre><p>O primeiro comando é opcional e consulta somente metadados com a configuração existente, sem gravar credenciais nos entregáveis. Os geradores escrevem somente em Doc/pipeline. Sem Git neste diretório, a versão é fixada por SHA-256 dos arquivos e do manifesto.</p>"
    body += "<h2>Modelo selecionado no mapa</h2><p>A camada padrão prioriza PFTHM medido e preenche ausências com o quadrático recalibrado, em todos os pontos com COD exato entre 1 e 159 mg/L. O ajuste é congelado e experimental; o mapa não retreina coeficientes. <a href='RECALIBRACAO_PFTHM.md'>Coeficientes, testes e integração</a>.</p>"
    body += "<h2>Modelos históricos IPT por COD</h2><p>As duas equações permanecem disponíveis para comparação. Fora de ANOR02300 (modelo 1) ou ARPE02800 (modelo 2), são extrapolações experimentais. A seleção antiga (modelo 2 em ARPE02800, modelo 1 nos demais) é preservada nas tabelas históricas; não preenche a camada padrão atual. Não há probabilidades de risco.</p>"
    body += "<p><a href='MODELOS_IPT_COD.md'>Equações, limites e avaliação temporal</a> · <a href='avaliacao_modelos_ipt.json'>Resultados numéricos da avaliação</a></p>"
    (OUT / "index.html").write_text(page("Pipeline de dados CETESB — documentação", body), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sem-reconciliar", action="store_true")
    parser.add_argument("--reutilizar-inventario", action="store_true")
    args = parser.parse_args()
    print("Inventariando fontes, partições e backups em leitura...", flush=True)
    snapshot_path = OUT / "inventario_local.json"
    if args.reutilizar_inventario:
        saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
        for obj in saved["objetos"]:
            path = ROOT / obj["objeto"].split("#", 1)[0]
            stat = path.stat()
            if stat.st_size != obj["bytes"] or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat() != obj["modificado_utc"]:
                raise ValueError("Artefato alterado desde o inventário: " + str(path))
        if saved["hashes_codigo"] != HASHES:
            raise ValueError("Código da pipeline mudou; refazer inventário.")
        manifest = saved["manifesto"]
        PHYSICAL.extend(saved["objetos"])
        SCHEMAS.update(saved["esquemas"])
        ISSUES.extend(saved["pendencias"])
        EXCEL_FILES.update(saved["excel"])
        for target, key in [(HEADERS, "headers"), (PARAMETERS, "parametros"), (UNITS, "unidades")]:
            target.update({k: set(v) for k, v in saved[key].items()})
        for table, fields in saved["tipos"].items():
            for col, types in fields.items():
                SCHEMA_TYPES[table][col].update(types)
        for target, key in [(PROFILES, "perfis"), (RAW_PROFILES, "perfis_por_ponto")]:
            target.update(saved[key])
            for fields in target.values():
                for info in fields.values():
                    info["dominio"] = set(info["dominio"])
        for table in ANALYTIC + ["cache_mapa_sp"]:
            FRAMES[table] = pd.read_parquet(ROOT / "resultados_v2" / (table + ".parquet"))
        ana = json.loads((ROOT / "ana_etas_sp.json").read_text(encoding="utf-8"))
        FRAMES["ana_captacoes_etas_sp"] = pd.DataFrame(ana["ana_etas"])
        FRAMES["relacao_cetesb_ana_etas"] = pd.DataFrame(ana["relations_by_station"].values())
        FRAMES["pontos_monitoramento"] = pd.read_csv(ROOT / "pontos_monitoramento.csv", dtype=str)
        print("Inventário reutilizado após conferir tamanho/data dos artefatos e hashes do código.", flush=True)
    else:
        manifest = inventory()
        snapshot = {"manifesto": manifest, "objetos": PHYSICAL, "esquemas": SCHEMAS, "pendencias": ISSUES,
                    "excel": EXCEL_FILES, "headers": HEADERS, "parametros": PARAMETERS, "unidades": UNITS,
                    "tipos": SCHEMA_TYPES, "perfis": PROFILES, "perfis_por_ponto": RAW_PROFILES, "hashes_codigo": HASHES}
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, default=lambda x: sorted(x) if isinstance(x, set) else str(x)), encoding="utf-8")
    # A avaliação congelada é independente das quatro tabelas históricas.
    calibration_table = "predicoes_pfthm_recalibradas"
    calibration_path = ROOT / "resultados_recalibracao" / MODEL_VERSION / (calibration_table + ".parquet")
    calibration_frame = pd.read_parquet(calibration_path)
    FRAMES[calibration_table] = calibration_frame
    profile(calibration_frame, calibration_table, calibration_path.relative_to(ROOT).as_posix())
    for col in calibration_frame:
        SCHEMA_TYPES[calibration_table][col].add(str(calibration_frame[col].dtype))
    for obj in PHYSICAL:
        if obj["objeto"] == calibration_path.relative_to(ROOT).as_posix():
            obj["tabela_logica"] = calibration_table
            obj["classe"] = "Avaliação de recalibração congelada"
    sql_path = OUT / "sql_inventario.json"
    sql = json.loads(sql_path.read_text(encoding="utf-8")) if sql_path.exists() else {}
    if not sql:
        issue("SQL-AUSENTE", "SQL", "Não há inventário SQL nesta execução.", "Executar inventariar_sql.py com acesso de leitura autorizado.", "Alta")
    catalog_rule = ("EXT-CATALOGO", "scraper_infoaguas.py", 180, 242, "InfoaguasScraper.fetch_catalog / _parse_catalog",
                    "Extrair o catálogo HTML e associar identificadores internos às linhas.",
                    "Tabela HTML e inputs ocultos do portal.", "Rejeita códigos ausentes, duplicados e desalinhados; renomeia cabeçalhos. O CSV local não prova completude.")
    RULES.append(catalog_rule)
    RULE_BY_ID[catalog_rule[0]] = catalog_rule
    DESCRIPTIONS.update({"data_inicio": DESCRIPTIONS["inicio_operacao"], "data_fim": DESCRIPTIONS["fim_operacao"]})
    tables, columns = build_catalog(sql)
    rules = build_rules(columns)
    issue("SQL-RAW-TIPOS", "664 tabelas SQL de ingestão", "Tabelas brutas antigas incluem VARCHAR e fim_operacao DATETIME, divergentes do contrato atual NVARCHAR/DATE.", "Preservar tipo observado no dicionário; não executar migração automática.", "Alta")
    issue("HISTORICO-EXTRA", "Ingestão SQL / old files", "Há 664 tabelas brutas SQL versus 659 fontes atuais na raiz; cinco arquivos históricos em old files são inventariados à parte.", "Confirmar política de períodos históricos e sobreposição antes de consolidar brutos SQL.")
    issue("IET-CLASSE", "IET", "O código exclui salobra/salina, mas não exclui automaticamente classe ausente; ambiente contendo lentico pode incluir intermediário.", "Revisão de domínio e elegibilidade; não alterar regra durante documentação.")
    issue("THM-UNIDADE", "estudo_thm_base.ecoli_ufc_100ml",
          "Unidade local observada: " + " | ".join(sorted(UNITS["escherichia_coli"])) + ". A rotina copia o resultado sem conversão microbiológica; não foi encontrada divergência com o nome nesta versão.",
          "Revisão preventiva: manter unidade da fonte e não inferir equivalência NMP/UFC em futuras cargas.")
    issue("ENV-PYARROW", "Ambiente", f"PyArrow instalado {importlib.metadata.version('pyarrow')}; requirements.txt declara 19.0.1.", "Registrar diferença de ambiente; dependências não alteradas.")
    issue("EXPORTACAO-HISTORICA", "cetesb_dados_brutos_consolidados.xlsx",
          "Duas abas históricas têm esquema inventariado e hash do arquivo, mas não declaram dimensões no XML. Valores e contagem integral não foram reconciliados.",
          "Não somar esta exportação às fontes atuais. Conferir seu histórico e contar linhas integralmente se necessário.")
    reconciliation = [] if args.sem_reconciliar else reconcile()
    coverage = {
        "gerado_em_utc": datetime.now(timezone.utc).isoformat(), "versao_dados": DATA_VERSION,
        "codigo_sha256": HASHES, "manifesto_sha256": digest(ROOT / "resultados_v2/manifesto.json"),
        "documentacao_codigo_sha256": {p.name: digest(p) for p in OUT.glob("*.py")},
        "tabelas_sql": len({(c["esquema"], c["tabela"]) for c in sql.get("colunas", [])}),
        "objetos_fisicos": len(PHYSICAL), "entradas_colunas": len(columns), "regras": len(rules), "pendencias": len(ISSUES),
        "colunas_com_descricao_nao_confirmada": sum("Pendente" in c["status"] for c in columns),
        "colunas_com_evidencia_codigo_atual": sum(bool(c["codigo"]) for c in columns),
        "colunas_sql_inventariadas": len(sql.get("colunas", [])),
        "colunas_sql_documentadas": sum("." in c["tabela"] for c in columns),
        "colunas_sem_definicao_funcional_atual": [c["tabela"] + "." + c["coluna"] for c in columns if c["status"] == "Pendente: significado funcional"],
        "sql_consulta_em_utc": sql.get("consultado_em_utc", "não consultado"),
        "escopo_validacao": "Inventário de esquemas integral local/SQL visível; valores locais perfilados integralmente nas tabelas atuais; reconciliação Excel em quatro pontos, incluindo ANOR02300 e ARPE02800; SQL só metadados neste gerador; comparação integral dos valores SQL registrada separadamente em resultados_v2/verificacao_modelos_pipeline.json; histórico não reconstruído.",
        "totais_manifesto": manifest["totais"],
        "dependencias_instaladas": {n: importlib.metadata.version(n) for n in ["pandas", "numpy", "pyarrow", "openpyxl", "sqlalchemy", "pyodbc"]},
    }
    trace = [{k: c[k] for k in ["tabela", "coluna", "fonte_original", "origem_imediata", "classificacao", "regra", "regras_desde_origem", "expressao", "codigo", "funcao", "codigo_sha256", "status"]} for c in columns]
    schema_rows = [{"esquema_id": sid, "ordinal": i, **f} for sid, fields in SCHEMAS.items() for i, f in enumerate(fields, 1)]
    data = {"cobertura": coverage, "tabelas": tables, "colunas": [{k: v for k, v in c.items() if not k.startswith("_")} for c in columns],
            "transformacoes": rules, "rastreabilidade": trace, "pendencias": ISSUES, "objetos": PHYSICAL, "esquemas": schema_rows}
    (OUT / "catalogo.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (OUT / "cobertura.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Gerando planilha e páginas para {len(columns)} ocorrências de colunas...", flush=True)
    workbook({"Tabelas": tables, "Colunas": columns, "Transformações": rules, "Rastreabilidade": trace,
              "Pendências": ISSUES, "Objetos": PHYSICAL, "Esquemas": schema_rows, "Validações": reconciliation})
    render(tables, columns, rules, coverage, reconciliation)
    print(json.dumps(coverage, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
