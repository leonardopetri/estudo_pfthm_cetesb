from collections import Counter, defaultdict
import hashlib
from datetime import datetime, timezone
from html.parser import HTMLParser
import importlib
import json
from pathlib import Path
import re
import sys
import unittest

import openpyxl
import pyarrow.parquet as pq

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])
        if "id" in attrs:
            self.ids.add(attrs["id"])


class DocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((OUT / "catalogo.json").read_text(encoding="utf-8"))
        cls.sql = json.loads((OUT / "sql_inventario.json").read_text(encoding="utf-8"))
        cls.columns = {(r["tabela"], r["coluna"]): r for r in cls.catalog["colunas"]}
        cls.rules = {r["id"]: r for r in cls.catalog["transformacoes"]}

    def test_unique_column_occurrences(self):
        self.assertEqual(len(self.columns), len(self.catalog["colunas"]))

    def test_every_visible_sql_column_is_in_dictionary(self):
        expected = {(c["esquema"] + "." + c["tabela"], c["coluna"]) for c in self.sql["colunas"]}
        actual = {key for key in self.columns if "." in key[0]}
        self.assertEqual(expected, actual)

    def test_all_current_parquet_columns_and_partitions_are_inventoried(self):
        objects = {r["objeto"]: r for r in self.catalog["objetos"]}
        for path in (ROOT / "resultados_v2").rglob("*.parquet"):
            relative = path.relative_to(ROOT).as_posix()
            self.assertIn(relative, objects)
            table = objects[relative]["tabela_logica"]
            for col in pq.read_schema(path).names:
                self.assertIn((table, col), self.columns)

    def test_dictionary_has_required_fields_and_no_invented_complete_status(self):
        required = ["descricao", "tipo_logico", "unidade_formato", "granularidade", "papel_chave",
                    "dominio", "nulos_regra", "origem_imediata", "fonte_original", "status"]
        for key, row in self.columns.items():
            for field in required:
                self.assertTrue(row.get(field), (key, field))
            if key[0].startswith("cetesb_backup_"):
                self.assertIn("Pendente", row["status"])
                self.assertFalse(row["codigo"])
            elif row["codigo"]:
                filename, span = row["codigo"].split(":")
                low, high = map(int, span.split("-"))
                source = ROOT / filename
                self.assertTrue(source.is_file(), key)
                self.assertLessEqual(high, len(source.read_text(encoding="utf-8-sig").splitlines()), key)
                self.assertGreaterEqual(low, 1, key)

    def test_rule_snippets_and_code_hashes_match_real_source(self):
        for row in self.rules.values():
            filename, span = row["codigo"].split(":")
            low, high = map(int, span.split("-"))
            source = ROOT / filename
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), row["codigo_sha256"])
            lines = source.read_text(encoding="utf-8-sig").splitlines()
            snippet = "\n".join(f"{i}: {line}" for i, line in enumerate(lines, 1) if low <= i <= high)
            self.assertEqual(row["trecho_real"], snippet)
        for row in self.columns.values():
            for rule in row["regra"].split(" | "):
                if rule:
                    self.assertIn(rule, self.rules)

    def test_internal_lineage_references_existing_columns(self):
        models = {r["tabela"] for r in self.catalog["tabelas"] if "." not in r["tabela"]}
        for key, row in self.columns.items():
            if "." in key[0]:
                continue
            for source in row["origem_imediata"].split(" | "):
                table, separator, col = source.partition(".")
                if separator and table in models:
                    self.assertIn((table, col), self.columns, (key, source))

    def test_schema_inventory_preserves_individual_objects(self):
        schemas = defaultdict(list)
        for field in self.catalog["esquemas"]:
            schemas[field["esquema_id"]].append(field)
        for obj in self.catalog["objetos"]:
            if obj["esquema_id"]:
                self.assertEqual(obj["quantidade_colunas"], len(schemas[obj["esquema_id"]]), obj["objeto"])
        count = Counter(c["tabela"] for c in self.catalog["colunas"])
        for table in self.catalog["tabelas"]:
            self.assertEqual(table["colunas"], count[table["tabela"]], table["tabela"])

    def test_sensitive_config_not_embedded(self):
        text = (OUT / "catalogo.json").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?i)(?:password|pwd|user\s*id|uid)\s*=\s*[^;\s]+")
        self.assertNotIn("AZURE_SQL_CONNECTION_STRING=", text)

    def test_local_navigation_targets_exist(self):
        ids = {}
        for path in OUT.glob("*.html"):
            parser = Links()
            parser.feed(path.read_text(encoding="utf-8"))
            ids[path.name] = parser.ids
            for link in parser.links:
                if "://" in link:
                    continue
                target, _, anchor = link.partition("#")
                target_path = (OUT / target) if target else path
                self.assertTrue(target_path.exists(), (path.name, link))
                if anchor:
                    if target_path.name not in ids:
                        other = Links()
                        other.feed(target_path.read_text(encoding="utf-8"))
                        ids[target_path.name] = other.ids
                    self.assertIn(anchor, ids[target_path.name], (path.name, link))

    def test_workbook_rows_and_formula_safety(self):
        book = openpyxl.load_workbook(OUT / "dicionario_pipeline.xlsx", read_only=True, data_only=False)
        expected = {"Tabelas": len(self.catalog["tabelas"]), "Colunas": len(self.columns),
                    "Transformações": len(self.rules), "Rastreabilidade": len(self.columns),
                    "Pendências": len(self.catalog["pendencias"]), "Objetos": len(self.catalog["objetos"]),
                    "Esquemas": len(self.catalog["esquemas"])}
        for name, count in expected.items():
            self.assertIn(name, book.sheetnames)
            actual = 0
            for row in book[name].iter_rows():
                actual += 1
                self.assertTrue(all(c.data_type != "f" for c in row), name)
            self.assertEqual(actual, count + 1, name)
        book.close()

    def test_reconciliations_executed(self):
        checks = json.loads((OUT / "reconciliacoes.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(checks), 10)
        self.assertTrue(all(r["status"] == "PASSOU" for r in checks))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromModule(importlib.import_module("test_validacao")),
        loader.loadTestsFromTestCase(DocumentationTests),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "executado_em_utc": datetime.now(timezone.utc).isoformat(),
        "testes_executados": result.testsRun,
        "sucesso": result.wasSuccessful(),
        "falhas": [str(test) for test, _ in result.failures],
        "erros": [str(test) for test, _ in result.errors],
        "ignorados": [str(test) for test, _ in result.skipped],
        "catalogo_sha256": hashlib.sha256((OUT / "catalogo.json").read_bytes()).hexdigest(),
        "escopo": "Testes da pipeline e estruturais/documentais, incluindo equações IPT COD; sem publicação ou consulta de valores SQL.",
    }
    (OUT / "verificacao_documentacao.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
