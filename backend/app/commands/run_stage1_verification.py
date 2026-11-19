"""Private assessment implementation and its producer are not distributed."""

def _supported_observation_source_sql(*args, **kwargs):
    raise RuntimeError("Private calculation producer is not distributed.")

def _validate_batch_arguments(*args, **kwargs):
    raise RuntimeError("Private calculation producer is not distributed.")

def main(*args, **kwargs):
    raise RuntimeError("Private calculation producer is not distributed.")

def run_verification(*args, **kwargs):
    raise RuntimeError("Private calculation producer is not distributed.")

def _mirror_verification_results_to_partitioned(*args, **kwargs):
    raise RuntimeError("Private calculation producer is not distributed.")

if __name__ == "__main__":
    raise SystemExit("Private calculation producer is not distributed.")
