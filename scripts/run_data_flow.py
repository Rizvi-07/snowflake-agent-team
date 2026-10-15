"""Products pipeline: validate, replace Bronze, then build/test Silver and Gold."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    # Support `python scripts/run_data_flow.py` from any working directory.
    sys.path.insert(0, str(ROOT))

from ingestion import ingest_products as bronze


PRODUCTS_SELECTOR = "+product_category_summary"
PASSPHRASE_ENV = "DBT_ENV_SECRET_PRODUCTS_FLOW_KEY_PASSPHRASE"


class FlowError(Exception):
    """Safe flow summary; raw subprocess/connector errors must not be printed."""


def run_dbt(command: str, profiles_dir: Path, target: str, target_path: Path, env: dict):
    args = [
        sys.executable, "-m", "dbt.cli.main", command,
        "--project-dir", str(ROOT / "dbt"), "--profiles-dir", str(profiles_dir),
        "--target", target, "--target-path", str(target_path),
        "--log-path", str(target_path / "logs"), "--no-partial-parse",
        "--no-send-anonymous-usage-stats", "--no-use-colors",
    ]
    if command == "build":
        args += ["--select", PRODUCTS_SELECTOR, "--indirect-selection", "eager"]
    # Never use a shell or echo child output: dbt/adapter errors may contain
    # SQL, row values or authentication details. Report structured status only.
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, check=False)


def validate_manifest(path: Path, project_name: str, database: str, schemas: dict) -> set[str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    nodes, sources = manifest["nodes"], manifest["sources"]
    source_id = f"source.{project_name}.bronze.products"
    silver_id = f"model.{project_name}.stg_products"
    gold_id = f"model.{project_name}.product_category_summary"
    source = sources[source_id]
    if (source["database"], source["schema"], source["identifier"]) != (database, schemas["BRONZE"], "PRODUCTS"):
        raise FlowError("dbt Bronze source does not match the validated destination.")
    for unique_id, layer in ((silver_id, "SILVER"), (gold_id, "GOLD")):
        if (nodes[unique_id]["database"], nodes[unique_id]["schema"]) != (database, schemas[layer]):
            raise FlowError("dbt model schemas do not match the validated destinations.")
        if nodes[unique_id]["alias"] != unique_id.rsplit('.', 1)[1]:
            raise FlowError("dbt model aliases do not match the reported destinations.")
    if set(nodes[silver_id]["depends_on"]["nodes"]) != {source_id} or set(nodes[gold_id]["depends_on"]["nodes"]) != {silver_id}:
        raise FlowError("Products dependency graph changed; refusing to include unrelated models.")
    graph = {source_id, silver_id, gold_id}
    tests = {
        n["unique_id"] for n in nodes.values() if n["resource_type"] == "test"
        and graph.intersection(n["depends_on"]["nodes"])
    }
    if not tests:
        raise FlowError("No products data tests found.")
    if any(not set(nodes[t]["depends_on"]["nodes"]).issubset(graph) for t in tests):
        raise FlowError("Products tests depend on nodes outside the products graph.")
    return {silver_id, gold_id, *tests}


def report_build(path: Path, expected_nodes: set[str]) -> bool:
    results = json.loads(path.read_text(encoding="utf-8"))["results"]
    statuses = Counter(r["status"] for r in results)
    allowed = {"success", "pass", "warn", "fail", "error", "skipped"}
    if not statuses.keys() <= allowed:
        raise FlowError("dbt returned an unexpected execution status.")
    print("dbt results: " + ", ".join(f"{name}={count}" for name, count in sorted(statuses.items())))
    return (
        len(results) == len(expected_nodes)
        and {r["unique_id"] for r in results} == expected_nodes
        and all(r["status"] == ("success" if r["unique_id"].startswith("model.") else "pass") for r in results)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("dev", "prod"), default="dev")
    parser.add_argument("--profiles-dir", type=Path, default=Path(os.environ.get("DBT_PROFILES_DIR", ROOT / "config" / "local")))
    parser.add_argument("--pr-number", help="Positive Dev PR number; isolates all three layers")
    parser.add_argument("--apply", action="store_true", help="Execute the pipeline; otherwise validate and report only")
    args = parser.parse_args(argv)
    stage = "configuration validation"
    bronze_committed = False
    failure_code = 1
    try:
        if args.apply:
            bronze.validate_execution(args.target)
        if args.target == "prod" and args.pr_number is not None:
            raise FlowError("--pr-number is supported only with --target dev.")
        project = yaml.safe_load((ROOT / "dbt" / "dbt_project.yml").read_text(encoding="utf-8"))
        raw = bronze.selected_profile(args.profiles_dir.resolve(), project["profile"], args.target)
        settings = bronze.render_settings(raw, target=args.target)
        table = bronze.destination(settings, args.pr_number, target=args.target)
        bronze_schema = table.split(".")[1].strip('"')
        prefix = bronze_schema[:-len("BRONZE")]
        schemas = {layer: prefix + layer for layer in ("BRONZE", "SILVER", "GOLD")}
        csv_path = ROOT / "sample_data" / "products.csv"
        rows = bronze.read_products(csv_path)
        print(f"Target: {args.target}; database: {settings['database']}")
        print(f"Bronze destination: {table}")
        print(f"Silver destination: {settings['database']}.{schemas['SILVER']}.stg_products")
        print(f"Gold destination: {settings['database']}.{schemas['GOLD']}.product_category_summary")
        print(f"Validated {len(rows)} product rows; dbt graph: {PRODUCTS_SELECTOR}")
        if not args.apply:
            print("DRY RUN: would replace Bronze, then build the products graph and data tests.")
            print("No connections or subprocesses. Credentials and dbt execution not checked.")
            return 0

        settings = bronze.render_settings(raw, credentials=True, target=args.target)
        # Freeze schema and identity once, so even literal-schema profiles and
        # --pr-number route ingestion and dbt identically. Passphrases stay in
        # the child environment, never command arguments or profile file text.
        env = os.environ.copy()
        env[PASSPHRASE_ENV] = settings["private_key_passphrase"]
        profile_output = {
            **raw, **settings, "schema": schemas["SILVER"],
            "private_key_path": str(Path(settings["private_key_path"]).expanduser().resolve()),
            "private_key_passphrase": "{{ env_var('" + PASSPHRASE_ENV + "') }}",
        }
        settings["private_key_path"] = profile_output["private_key_path"]
        with tempfile.TemporaryDirectory(prefix="products-flow-") as directory:
            temporary = Path(directory)
            profile_file = temporary / "profiles.yml"
            profile_file.write_text(yaml.safe_dump({
                project["profile"]: {"target": args.target, "outputs": {args.target: profile_output}}
            }), encoding="utf-8")
            profile_file.chmod(0o600)
            target_path = temporary / "target"
            # Parse and verify the exact source/model graph before modifying data.
            parsed = run_dbt("parse", temporary, args.target, target_path, env)
            if parsed.returncode:
                raise FlowError("dbt configuration parsing failed; Bronze was not modified.")
            expected_nodes = validate_manifest(target_path / "manifest.json", project["name"], settings["database"], schemas)
            print("Configuration and dbt destinations verified.")

            stage = "Bronze ingestion"
            connection = bronze.connect(settings, target=args.target)
            try:
                batch_id = bronze.replace_products(connection, table, rows, csv_path.name, target=args.target)
                bronze_committed = True
            finally:
                connection.close()
            print(f"Bronze committed and verified: {len(rows)} rows; batch_id={batch_id}")

            stage = "dbt products build/tests"
            built = run_dbt("build", temporary, args.target, target_path, env)
            results_file = target_path / "run_results.json"
            verified = report_build(results_file, expected_nodes) if results_file.is_file() else False
            if built.returncode or not verified:
                raise FlowError("dbt products build/tests failed or returned incomplete results.")
            print("APPLIED: products flow succeeded.")
        return 0
    except (bronze.IngestionError, FlowError) as exc:
        print(f"ERROR during {stage}: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print(f"ERROR: interrupted during {stage}.", file=sys.stderr)
        failure_code = 130
    except Exception:
        print(f"ERROR during {stage}; private diagnostics were suppressed.", file=sys.stderr)
    if bronze_committed:
        print("Bronze was committed; later failure does not roll back earlier pipeline stages.", file=sys.stderr)
    return failure_code


if __name__ == "__main__":
    raise SystemExit(main())
