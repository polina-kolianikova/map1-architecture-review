import unittest

from app.commands.run_stage3_scale_hardening import INDEX_DDL
from app.commands.run_stage3_scale_hardening import _blockers, _table_stats


class Stage3ScaleHardeningTest(unittest.TestCase):
    def test_grid_cell_id_order_index_is_required_for_large_asset_register_pages(self):
        ddl = INDEX_DDL["grid_cells_region_version_id_idx"]

        self.assertIn("ON grid_cells (region_id, grid_version, id)", ddl)
        self.assertIn("INCLUDE (uuid)", ddl)

    def test_blocks_missing_indexes(self):
        blockers = _blockers(
            {
                "indexes": {
                    "grid_cells_geom_gix": False,
                    "ogm_parcels_geom_gix": True,
                },
                "partitions": {},
            },
            require_partitions=False,
            allow_shadow_partitions=False,
        )

        self.assertEqual(blockers, ["missing indexes: grid_cells_geom_gix"])

    def test_blocks_missing_partitions_when_required(self):
        blockers = _blockers(
            {
                "indexes": {
                    "grid_cells_geom_gix": True,
                },
                "partitions": {
                    "grid_cells": True,
                    "sentinel_observations": False,
                    "verification_results": False,
                },
                "shadow_partitions": {},
                "shadow_partition_counts_match": {},
            },
            require_partitions=True,
            allow_shadow_partitions=False,
        )

        self.assertEqual(
            blockers,
            [
                "missing region partitions: sentinel_observations, verification_results; "
                "generate the rollout with run_stage3_partition_rollout"
            ],
        )

    def test_allows_partition_plan_to_be_optional_for_public_scope(self):
        blockers = _blockers(
            {
                "indexes": {
                    "grid_cells_geom_gix": True,
                },
                "partitions": {
                    "grid_cells": False,
                },
            },
            require_partitions=False,
            allow_shadow_partitions=False,
        )

        self.assertEqual(blockers, [])

    def test_blocks_validated_shadow_partitions_when_required_for_active_cutover(self):
        blockers = _blockers(
            {
                "indexes": {
                    "grid_cells_geom_gix": True,
                },
                "partitions": {
                    "grid_cells": False,
                    "sentinel_observations": False,
                    "verification_results": False,
                },
                "shadow_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
                "shadow_partition_counts_match": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            require_partitions=True,
            allow_shadow_partitions=False,
        )

        self.assertEqual(
            blockers,
            [
                "missing region partitions: grid_cells, sentinel_observations, verification_results; "
                "generate the rollout with run_stage3_partition_rollout"
            ],
        )

    def test_allows_validated_shadow_partitions_for_public_rollout(self):
        blockers = _blockers(
            {
                "indexes": {
                    "grid_cells_geom_gix": True,
                },
                "partitions": {
                    "grid_cells": False,
                    "sentinel_observations": False,
                    "verification_results": False,
                },
                "shadow_partitions": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
                "shadow_partition_counts_match": {
                    "grid_cells": True,
                    "sentinel_observations": True,
                    "verification_results": True,
                },
            },
            require_partitions=True,
            allow_shadow_partitions=True,
        )

        self.assertEqual(blockers, [])

    def test_table_stats_uses_pg_stat_relname(self):
        class Result:
            def __init__(self, rows=None, scalar=None):
                self.rows = rows or []
                self.scalar = scalar

            def mappings(self):
                return self

            def all(self):
                return self.rows

            def scalar_one(self):
                return self.scalar

        class Db:
            statements = []

            def execute(self, statement, params=None):
                self.statements.append(str(statement))
                if "count(*)" in str(statement):
                    return Result(scalar=12)
                return Result(rows=[{"table_name": "grid_cells", "estimated_rows": 12, "estimated_dead_rows": 0}])

        db = Db()
        stats = _table_stats(db)

        self.assertEqual(stats["grid_cells"]["estimated_rows"], 12)
        self.assertEqual(stats["grid_cells"]["row_count"], 12)
        self.assertIn("relname AS table_name", db.statements[0])
        self.assertIn("relname = ANY", db.statements[0])
        self.assertNotIn("table_name = ANY", db.statements[0])


if __name__ == "__main__":
    unittest.main()
