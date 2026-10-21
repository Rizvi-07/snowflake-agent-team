"""Offline dbt compilation and translated SQL checks, never Snowflake execution.

Run: python -m unittest discover -s tests -p 'test_*.py' -v
Dependencies: ingestion/requirements.txt and tests/requirements.txt.
"""

import csv
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import json
import random
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dbt.cli.main import dbtRunner
import duckdb
import sqlglot
from sqlglot import exp
import yaml

from scripts.run_data_flow import PRODUCTS_SELECTOR, validate_manifest


ROOT = Path(__file__).resolve().parents[1]
COLUMNS = ("product_id", "product_name", "category", "price", "source_filename", "batch_id", "ingested_at")
TIMESTAMP = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class ProductsDbtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifests = {}
        with tempfile.TemporaryDirectory() as directory:
            profiles_dir = Path(directory)
            # Compile with dummy credentials, not the user's local profile.
            # Also block connector.connect so an unexpected adapter lookup fails.
            for name, target, database, schema in (
                ("dev", "dev", "AGENTWAREHOUSEDEV", "SILVER"),
                ("pr", "dev", "AGENTWAREHOUSEDEV", "PR_42_SILVER"),
                ("prod", "prod", "AGENTWAREHOUSEPROD", "SILVER"),
                ("pr_bronze", "dev", "AGENTWAREHOUSEDEV", "PR_42_BRONZE"),
                ("shared_bronze", "dev", "AGENTWAREHOUSEDEV", "BRONZE"),
            ):
                output = dict(
                    type="snowflake", account="offline.invalid", user="OFFLINE_ONLY",
                    password="offline-not-a-credential", role="OFFLINE_ONLY",
                    database=database, warehouse="OFFLINE_ONLY", schema=schema, threads=2,
                )
                (profiles_dir / "profiles.yml").write_text(yaml.safe_dump({
                    "agent_platform": {"target": target, "outputs": {target: output}}
                }), encoding="utf-8")
                target_path = ROOT / "dbt" / "target" / "offline" / name
                common = [
                    "--project-dir", str(ROOT / "dbt"), "--profiles-dir", str(profiles_dir),
                    "--target", target, "--target-path", str(target_path),
                    "--no-partial-parse", "--no-send-anonymous-usage-stats", "--no-use-colors",
                    "--log-level", "warn",
                ]
                with patch("snowflake.connector.connect", side_effect=AssertionError("Offline check attempted Snowflake connection")) as connect:
                    for command in (
                        ["parse"],
                        ["compile", "--no-introspect", "--no-populate-cache", "--select", PRODUCTS_SELECTOR, "connection_check"],
                    ):
                        result = dbtRunner().invoke(command + common)
                        if not result.success:
                            raise AssertionError(f"Offline {command[0]} failed for {name}: {result.exception}")
                    if connect.call_count:
                        raise AssertionError("Offline checks attempted a Snowflake connection")
                cls.manifests[name] = json.loads((target_path / "manifest.json").read_text(encoding="utf-8"))
        cls.nodes = cls.manifests["dev"]["nodes"]
        cls.data_tests = {
            n["name"]: n for n in cls.nodes.values()
            if n["resource_type"] == "test" and n.get("compiled_code")
            and "connection_check" not in n["name"]
        }

    def setUp(self):
        self.connection = duckdb.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute("""
            create table bronze_products (
                product_id varchar, product_name varchar, category varchar, price varchar,
                source_filename varchar, batch_id varchar, ingested_at timestamptz
            )
        """)

    @staticmethod
    def local_sql(code):
        """Translate the actual compiled queries and map their relations locally."""
        parsed = sqlglot.parse_one(code, read="snowflake")
        relations = {"products": "bronze_products", "stg_products": "stg_products", "product_category_summary": "product_category_summary"}

        def map_relation(node):
            if isinstance(node, exp.Table) and node.name.lower() in relations:
                return exp.Table(this=exp.to_identifier(relations[node.name.lower()]), alias=node.args.get("alias"))
            return node

        return parsed.transform(map_relation).sql(dialect="duckdb", unsupported_level=sqlglot.ErrorLevel.RAISE)

    def model_sql(self, name):
        return self.local_sql(self.nodes[f"model.agent_platform.{name}"]["compiled_code"])

    def fixture_rows(self):
        with (ROOT / "sample_data" / "products.csv").open(encoding="utf-8", newline="") as handle:
            return [(*row.values(), "products.csv", "offline-batch", TIMESTAMP) for row in csv.DictReader(handle)]

    def build(self, rows=None):
        self.connection.executemany("insert into bronze_products values (?, ?, ?, ?, ?, ?, ?)", rows if rows is not None else self.fixture_rows())
        self.connection.execute("create table stg_products as " + self.model_sql("stg_products"))
        self.connection.execute("create table product_category_summary as " + self.model_sql("product_category_summary"))

    def failures(self):
        return {name for name, node in self.data_tests.items() if self.connection.execute(self.local_sql(node["compiled_code"])).fetchall()}

    def test_manifest_routes_sources_models_and_connection_check(self):
        for scenario, manifest in self.manifests.items():
            with self.subTest(scenario=scenario):
                database = "AGENTWAREHOUSEPROD" if scenario == "prod" else "AGENTWAREHOUSEDEV"
                prefix = "PR_42_" if scenario in ("pr", "pr_bronze") else ""
                expected_connection = {"pr": "PR_42_SILVER", "pr_bronze": "PR_42_BRONZE", "shared_bronze": "BRONZE"}.get(scenario, "SILVER")
                source = manifest["sources"]["source.agent_platform.bronze.products"]
                self.assertEqual((source["database"], source["schema"], source["identifier"]), (database, prefix + "BRONZE", "PRODUCTS"))
                for model, schema in (("stg_products", prefix + "SILVER"), ("product_category_summary", prefix + "GOLD"), ("connection_check", expected_connection)):
                    node = manifest["nodes"][f"model.agent_platform.{model}"]
                    self.assertEqual((node["database"], node["schema"]), (database, schema))

    def test_products_data_tests_are_compiled_and_selected(self):
        self.assertEqual(len(self.data_tests), 37)
        self.assertIn("bronze_to_silver_row_count", self.data_tests)
        self.assertIn("gold_matches_silver", self.data_tests)
        self.assertIn("gold_price_share_pct_in_range", self.data_tests)
        self.assertIn("gold_price_share_pct_boundaries", self.data_tests)

    def test_entrypoint_preflight_matches_the_explicit_products_graph(self):
        for scenario, manifest in self.manifests.items():
            with self.subTest(scenario=scenario):
                prefix = "PR_42_" if scenario in ("pr", "pr_bronze") else ""
                database = "AGENTWAREHOUSEPROD" if scenario == "prod" else "AGENTWAREHOUSEDEV"
                path = ROOT / "dbt" / "target" / "offline" / scenario / "manifest.json"
                selected = validate_manifest(path, "agent_platform", database, {layer: prefix + layer for layer in ("BRONZE", "SILVER", "GOLD")})
                self.assertEqual(len(selected), 39)
                self.assertFalse(any("connection_check" in node or "my_first_dbt_model" in node or "my_second_dbt_model" in node for node in selected))

    def test_five_product_fixture_and_every_data_test(self):
        self.build()
        self.assertEqual(self.failures(), set())
        rows = self.connection.execute("select * from product_category_summary order by category").fetchall()
        self.assertEqual(rows, [
            ("Electronics", 3, Decimal("1199.39"), Decimal("399.796667"), Decimal("49.90"), Decimal("899.99"), Decimal("80.05"), Decimal("850.09")),
            ("Furniture", 2, Decimal("298.90"), Decimal("149.450000"), Decimal("99.90"), Decimal("199.00"), Decimal("19.95"), Decimal("99.10")),
        ])
        self.assertEqual(self.connection.execute("select typeof(price_share_pct) from product_category_summary limit 1").fetchone()[0], "DECIMAL(5,2)")
        self.assertEqual(self.connection.execute("select typeof(price) from stg_products limit 1").fetchone()[0], "DECIMAL(18,2)")

    def test_single_positive_category_has_full_price_share(self):
        rows = [row for row in self.fixture_rows() if row[2] == "Furniture"]
        self.build(rows)
        self.assertEqual(self.failures(), set())
        self.assertEqual(self.connection.execute("select category, price_share_pct from product_category_summary").fetchall(), [("Furniture", Decimal("100.00"))])

    def test_zero_total_price_shares_are_zero_without_division_error(self):
        rows = [
            ("1", "Free cable", "Electronics", "0.00", "products.csv", "offline-batch", TIMESTAMP),
            ("2", "Free stool", "Furniture", "0", "products.csv", "offline-batch", TIMESTAMP),
        ]
        self.build(rows)
        self.assertEqual(self.failures(), set())
        self.assertEqual(self.connection.execute("select category, price_share_pct from product_category_summary order by category").fetchall(), [
            ("Electronics", Decimal("0.00")), ("Furniture", Decimal("0.00")),
        ])

    def test_price_shares_with_a_zero_category_stay_within_bounds(self):
        rows = [
            ("1", "Free cable", "Electronics", "0.00", "products.csv", "offline-batch", TIMESTAMP),
            ("2", "Desk", "Furniture", "10.00", "products.csv", "offline-batch", TIMESTAMP),
        ]
        self.build(rows)
        self.assertEqual(self.failures(), set())
        self.assertEqual(self.connection.execute("select category, price_share_pct from product_category_summary order by category").fetchall(), [
            ("Electronics", Decimal("0.00")), ("Furniture", Decimal("100.00")),
        ])

    def test_single_product_category_has_zero_price_range(self):
        rows = [
            ("1", "Laptop", "Electronics", "899.99", "products.csv", "offline-batch", TIMESTAMP),
            ("2", "Desk", "Furniture", "199.00", "products.csv", "offline-batch", TIMESTAMP),
            ("3", "Chair", "Furniture", "99.90", "products.csv", "offline-batch", TIMESTAMP),
        ]
        self.build(rows)
        self.assertEqual(self.failures(), set())
        self.assertEqual(self.connection.execute("select category, price_range from product_category_summary order by category").fetchall(), [
            ("Electronics", Decimal("0.00")), ("Furniture", Decimal("99.10")),
        ])

    def test_equal_price_category_has_zero_price_range(self):
        rows = [
            ("1", "Mouse", "Electronics", "25.50", "products.csv", "offline-batch", TIMESTAMP),
            ("2", "Cable", "Electronics", "25.5", "products.csv", "offline-batch", TIMESTAMP),
            ("3", "Hub", "Electronics", "25.50", "products.csv", "offline-batch", TIMESTAMP),
            ("4", "Stool", "Furniture", "0", "products.csv", "offline-batch", TIMESTAMP),
            ("5", "Bench", "Furniture", "0.00", "products.csv", "offline-batch", TIMESTAMP),
        ]
        self.build(rows)
        self.assertEqual(self.failures(), set())
        self.assertEqual(self.connection.execute("select category, price_range from product_category_summary order by category").fetchall(), [
            ("Electronics", Decimal("0.00")), ("Furniture", Decimal("0.00")),
        ])

    def test_price_range_data_tests_detect_invalid_values(self):
        self.build()
        self.connection.execute("update product_category_summary set price_range = -0.01 where category = 'Furniture'")
        self.assertTrue({"gold_matches_silver", "nonnegative_product_category_summary_price_range"} <= self.failures())
        self.connection.execute("update product_category_summary set price_range = null where category = 'Furniture'")
        self.assertTrue({"gold_matches_silver", "not_null_product_category_summary_price_range"} <= self.failures())

    def test_price_share_data_tests_detect_out_of_range_values(self):
        self.build()
        self.connection.execute("update product_category_summary set price_share_pct = 100.01 where category = 'Electronics'")
        self.assertIn("gold_price_share_pct_in_range", self.failures())
        self.connection.execute("update product_category_summary set price_share_pct = -0.01 where category = 'Electronics'")
        self.assertTrue({"gold_price_share_pct_in_range", "nonnegative_product_category_summary_price_share_pct"} <= self.failures())
        self.connection.execute("update product_category_summary set price_share_pct = null where category = 'Electronics'")
        self.assertIn("not_null_product_category_summary_price_share_pct", self.failures())

    def test_price_share_boundary_test_runs_the_model_expression(self):
        model = self.nodes["model.agent_platform.product_category_summary"]["raw_code"]
        expression = model.split("-- price_share_pct:begin")[1].split("-- price_share_pct:end")[0]
        boundary = self.data_tests["gold_price_share_pct_boundaries"]["compiled_code"]
        self.assertEqual(boundary.count(expression), 1)
        self.build()
        self.assertEqual(self.connection.execute(self.local_sql(boundary)).fetchall(), [])
        # The PR #25 defect: Snowflake keeps a few quotient decimals before the
        # final round, so 0.004999999975 became 0.00500000 and then 0.01.
        defective = "case when overall_total_price = 0 then 0 else round(round(total_price * 100 / overall_total_price, 8), 2) end"
        failing = self.connection.execute(self.local_sql(boundary.replace(expression, defective))).fetchall()
        self.assertEqual({(row[0], row[1]) for row in failing}, {("below_half", "Electronics")})

    def test_price_shares_match_exact_half_up_rounding(self):
        def expected(total, overall):
            if overall == 0:
                return Decimal("0.00")
            hundredths = Fraction(total) * 10000 / Fraction(overall)
            return Decimal(int(hundredths + Fraction(1, 2))) / 100

        generator = random.Random(19)
        for trial in range(25):
            prices = [Decimal(generator.choice((0, 1, 5, generator.randrange(1, 10 ** generator.randrange(1, 9))))) / 100 for _ in range(generator.randrange(2, 7))]
            rows = [(str(i + 1), f"Item {i + 1}", ("Electronics", "Furniture")[i % 2], str(price), "products.csv", "offline-batch", TIMESTAMP) for i, price in enumerate(prices)]
            with self.subTest(trial=trial, prices=prices):
                self.connection.execute("drop table if exists product_category_summary")
                self.connection.execute("drop table if exists stg_products")
                self.connection.execute("delete from bronze_products")
                self.build(rows)
                self.assertEqual(self.failures(), set())
                totals = {"Electronics": sum(prices[0::2]), "Furniture": sum(prices[1::2])}
                actual = dict(self.connection.execute("select category, price_share_pct from product_category_summary").fetchall())
                self.assertEqual(actual, {category: expected(total, sum(totals.values())) for category, total in totals.items()})

    def test_gold_reconciliation_accepts_only_the_correctly_rounded_share(self):
        self.build()
        for share, fails in (("80.04", True), ("80.05", False), ("80.06", True)):
            with self.subTest(share=share):
                self.connection.execute(f"update product_category_summary set price_share_pct = {share} where category = 'Electronics'")
                self.assertEqual("gold_matches_silver" in self.failures(), fails)

    def test_normalization_and_metadata_preservation(self):
        self.build([("001", "  Laptop  ", " eLECTRONICS ", "899.99", "file.csv", "batch-123", TIMESTAMP)])
        self.assertEqual(self.connection.execute("select * from stg_products").fetchone(), (1, "Laptop", "Electronics", Decimal("899.99"), "file.csv", "batch-123", TIMESTAMP))
        self.assertEqual(self.failures(), set())

    def test_invalid_rows_are_preserved_and_caught_by_data_tests(self):
        cases = [
            ({"product_id": "not-an-id"}, "not_null_stg_products_product_id"),
            ({"product_id": "1.5"}, "not_null_stg_products_product_id"),
            ({"product_id": "9" * 40}, "not_null_stg_products_product_id"),
            ({"price": "bad-price"}, "not_null_stg_products_price"),
            ({"price": "1.005"}, "not_null_stg_products_price"),
            ({"price": "-0.001"}, "not_null_stg_products_price"),
            ({"price": "1e2"}, "not_null_stg_products_price"),
            ({"price": "E"}, "not_null_stg_products_price"),
            ({"price": "9" * 40}, "not_null_stg_products_price"),
            ({"price": "-0.01"}, "nonnegative_stg_products_price"),
            ({"product_name": "  "}, "not_null_stg_products_product_name"),
            ({"category": "Unknown"}, "accepted_values_stg_products_category__Electronics__Furniture"),
            ({"category": "  "}, "not_null_stg_products_category"),
            ({"source_filename": None}, "not_null_stg_products_source_filename"),
            ({"batch_id": None}, "not_null_stg_products_batch_id"),
            ({"ingested_at": None}, "not_null_stg_products_ingested_at"),
        ]
        base = dict(zip(COLUMNS, self.fixture_rows()[0]))
        for override, expected_failure in cases:
            with self.subTest(override=override):
                self.connection.execute("delete from bronze_products")
                self.connection.execute("drop table if exists stg_products")
                self.connection.execute("drop table if exists product_category_summary")
                row = {**base, **override}
                self.build([tuple(row[column] for column in COLUMNS)])
                self.assertEqual(self.connection.execute("select count(*) from stg_products").fetchone()[0], 1)
                self.assertEqual(self.connection.execute("select sum(product_count) from product_category_summary").fetchone()[0], 1)
                self.assertIn(expected_failure, self.failures())

    def test_cent_accuracy_and_maximum_integer_id(self):
        rows = [
            ("9" * 38, "Exact cents A", "Electronics", "0.10", "products.csv", "offline-batch", TIMESTAMP),
            ("2", "Exact cents B", "Electronics", "0.20", "products.csv", "offline-batch", TIMESTAMP),
        ]
        self.build(rows)
        self.assertEqual(self.failures(), set())
        self.assertEqual(self.connection.execute("select max(product_id) from stg_products").fetchone()[0], Decimal("9" * 38))
        self.assertEqual(self.connection.execute("select total_price, average_price from product_category_summary").fetchone(), (Decimal("0.30"), Decimal("0.150000")))

    def test_duplicate_integer_ids_are_not_deduplicated(self):
        rows = self.fixture_rows()
        rows.append(("01", "Duplicate", "Electronics", "1.00", "products.csv", "offline-batch", TIMESTAMP))
        self.build(rows)
        self.assertEqual(self.connection.execute("select count(*) from stg_products").fetchone()[0], 6)
        self.assertIn("unique_stg_products_product_id", self.failures())

    def test_row_count_test_detects_dropped_silver_record(self):
        self.build()
        self.connection.execute("delete from stg_products where product_id = 1")
        self.assertIn("bronze_to_silver_row_count", self.failures())

    def test_gold_reconciliation_detects_each_corrupted_metric(self):
        self.build()
        for column in ("product_count", "total_price", "average_price", "minimum_price", "maximum_price", "price_share_pct", "price_range"):
            with self.subTest(column=column):
                self.connection.execute("delete from product_category_summary")
                self.connection.execute("insert into product_category_summary " + self.model_sql("product_category_summary"))
                self.connection.execute(f"update product_category_summary set {column} = {column} + 1 where category = 'Electronics'")
                self.assertIn("gold_matches_silver", self.failures())

    def test_gold_reconciliation_detects_missing_and_extra_categories(self):
        self.build()
        self.connection.execute("delete from product_category_summary where category = 'Electronics'")
        self.assertIn("gold_matches_silver", self.failures())
        self.connection.execute("delete from product_category_summary")
        self.connection.execute("insert into product_category_summary " + self.model_sql("product_category_summary"))
        self.connection.execute("insert into product_category_summary values ('Other', 1, 1, 1, 1, 1, 0, 0)")
        self.assertIn("gold_matches_silver", self.failures())


if __name__ == "__main__":
    unittest.main()
