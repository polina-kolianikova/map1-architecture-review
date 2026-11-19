import unittest

from app.commands.run_stage3_partition_rollout import (
    build_partition_rollout_plan,
    build_partition_sql,
)


def table_state(*, rows: int = 0, has_region_id: bool = True) -> dict[str, dict]:
    return {
        "grid_cells": {"exists": True, "has_region_id": has_region_id, "estimated_rows": rows},
        "sentinel_observations": {"exists": True, "has_region_id": has_region_id, "estimated_rows": rows},
        "verification_results": {"exists": True, "has_region_id": has_region_id, "estimated_rows": rows},
    }


class Stage3PartitionRolloutTest(unittest.TestCase):
    def test_blocks_non_empty_unpartitioned_tables_without_shadow_rollout(self):
        report = build_partition_rollout_plan(
            table_state=table_state(rows=100),
            regions=[{"id": 7, "code": "TR-BOLU", "name": "Bolu"}],
            existing_partitions={
                "grid_cells": False,
                "sentinel_observations": False,
                "verification_results": False,
            },
            allow_online_shadow=False,
            emit_sql=False,
        )

        self.assertFalse(report["ready_to_apply"])
        self.assertIn(
            "non-empty FK-bound tables require shadow rollout: "
            "grid_cells, sentinel_observations, verification_results",
            report["blockers"],
        )

    def test_allows_online_shadow_rollout_for_non_empty_tables(self):
        report = build_partition_rollout_plan(
            table_state=table_state(rows=100),
            regions=[{"id": 7, "code": "TR-BOLU", "name": "Bolu"}],
            existing_partitions={
                "grid_cells": False,
                "sentinel_observations": False,
                "verification_results": False,
            },
            allow_online_shadow=True,
            emit_sql=True,
        )

        self.assertTrue(report["ready_to_apply"])
        self.assertEqual(report["blockers"], [])
        self.assertFalse(report["cutover_ready"])
        self.assertIn("active tables are not region-partitioned", report["cutover_blockers"][0])
        self.assertTrue(any("grid_cells_partitioned" in statement for statement in report["sql"]))

    def test_cutover_ready_only_when_active_tables_are_partitioned(self):
        report = build_partition_rollout_plan(
            table_state=table_state(rows=100),
            regions=[{"id": 7, "code": "TR-BOLU", "name": "Bolu"}],
            existing_partitions={
                "grid_cells": True,
                "sentinel_observations": True,
                "verification_results": True,
            },
            allow_online_shadow=True,
            emit_sql=False,
        )

        self.assertTrue(report["cutover_ready"])
        self.assertEqual(report["cutover_blockers"], [])

    def test_blocks_missing_region_scope_columns(self):
        report = build_partition_rollout_plan(
            table_state=table_state(rows=0, has_region_id=False),
            regions=[{"id": 7, "code": "TR-BOLU", "name": "Bolu"}],
            existing_partitions={
                "grid_cells": False,
                "sentinel_observations": False,
                "verification_results": False,
            },
            allow_online_shadow=False,
            emit_sql=False,
        )

        self.assertFalse(report["ready_to_apply"])
        self.assertIn(
            "missing region_id columns: grid_cells, sentinel_observations, verification_results",
            report["blockers"],
        )

    def test_sql_uses_region_partitions_and_child_backfill_by_grid_cell_region(self):
        statements = build_partition_sql([{"id": 7, "code": "TR-BOLU"}])

        self.assertIn(
            "CREATE TABLE IF NOT EXISTS grid_cells_partitioned_tr_bolu "
            "PARTITION OF grid_cells_partitioned FOR VALUES IN (7)",
            statements,
        )
        self.assertFalse(any("INCLUDING CONSTRAINTS" in statement for statement in statements))
        self.assertTrue(
            any(
                "INSERT INTO sentinel_observations_partitioned SELECT source.*"
                in statement
                and "JOIN grid_cells cell ON cell.id = source.grid_cell_id" in statement
                for statement in statements
            )
        )
        self.assertIn("TRUNCATE grid_cells_partitioned", statements)
        self.assertIn("ANALYZE grid_cells_partitioned", statements)


if __name__ == "__main__":
    unittest.main()
