"""Execute only this region's reviewed build stages using the shared staging dir."""
from pathlib import Path
import os
import subprocess
import sys

root = Path(os.environ["ANALYTICAL_REGION_ROOT"])
output = Path(os.environ["ANALYTICAL_OUTPUT_DIR"])
pipeline = root / "pipeline"


def run(script, *args):
    subprocess.run([sys.executable, str(pipeline / script), *map(str, args)], check=True)


run("prepare_inputs.py", "--baseline", root / "sources/baseline", "--sources", root / "sources", "--output", output, "--legacy-only")
run("build_priority.py", "--config", "pipeline/priority_config.json")
run("build_density.py", "--input", output, "--osm-cache", root / "sources/osm-cache", "--output", output,
    "--building-review", root / "sources/building-review.json", "--housing-register", root / "review/housing-register.json", "--source-policy", "legacy_only")
run("audit_density.py", "--data", output, "--output", output / "density-acceptance.json", "--strict")
run("sensitivity.py", "--data", output)
