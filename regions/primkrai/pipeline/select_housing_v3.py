"""Select a validated local housing inventory without changing density inputs."""
import argparse
import json
from pathlib import Path
from verify_housing_audit import checksum
from verify_housing_v3 import verify_v3


def select(root,out):
    root=root.resolve();out=out.resolve();verification=verify_v3(root,out)
    pointer={'schema_version':3,'dataset':str(out.relative_to(root)),
        'inventory':str((out/'housing-inventory-v3.jsonl').relative_to(root)),
        'integrity_manifest_sha256':checksum(out/'integrity-manifest.json'),
        'verification':verification,'role':'current_housing_evidence_inventory',
        'population_model_policy':'LEGACY_ONLY_OFFLINE',
        'population_weight_overrides_applied':False,
        'limitation':'Address candidates do not confirm cadastral geometry or current occupancy'}
    target=root/'sources/housing-current.json';temporary=target.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(pointer,ensure_ascii=False,indent=2)+'\n');temporary.replace(target)
    return pointer


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--dataset',type=Path,required=True);args=parser.parse_args()
    result=select(Path(__file__).resolve().parents[1],args.dataset);print(json.dumps(result,ensure_ascii=False))
