"""Private assessment implementation and its producer are not distributed."""

def main(*args, **kwargs):
    raise RuntimeError("Private calculation producer is not distributed.")

def _validate_args(*args, **kwargs):
    raise RuntimeError("Private calculation producer is not distributed.")

def _run_backfill_batches(*args, **kwargs):
    raise RuntimeError("Private calculation producer is not distributed.")

def _backfill_preview_sql(*args, **kwargs):
    raise RuntimeError("Private calculation producer is not distributed.")

def _backfill_update_sql(*args, **kwargs):
    raise RuntimeError("Private calculation producer is not distributed.")

def _backfill_ready_cte_sql(*args, **kwargs):
    raise RuntimeError("Private calculation producer is not distributed.")

if __name__ == "__main__":
    raise SystemExit("Private calculation producer is not distributed.")
