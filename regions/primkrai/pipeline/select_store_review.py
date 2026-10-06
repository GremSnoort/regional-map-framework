"""Select a validated local store catalog review, retaining the published layer."""
import argparse,json
from pathlib import Path
from verify_housing_audit import checksum
from verify_store_catalog_audit import verify_stores

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--dataset',type=Path,required=True);a=parser.parse_args();root=Path(__file__).resolve().parents[1];dataset=a.dataset.resolve()
    verification=verify_stores(root,dataset)
    pointer={'schema_version':1,'role':'current_store_catalog_review','observed_on':'2026-10-06','dataset':str(dataset.relative_to(root)),
        'stores':str((dataset/'stores-refined.geojson').relative_to(root)),'integrity_manifest_sha256':checksum(dataset/'integrity-manifest.json'),
        'verification':verification,'active_layer_replaced':False,'operating_status_confirmed':False,'coordinate_confirmation':False,
        'review_scripts_sha256':{name:checksum(root/'pipeline'/name) for name in ['verify_store_catalog_audit.py','select_store_review.py','test_store_catalog_audit.py']}}
    path=root/'sources/stores-current.json';tmp=path.with_suffix('.json.tmp');tmp.write_text(json.dumps(pointer,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)
    (dataset/'verification.json').write_text(json.dumps(verification,ensure_ascii=False,indent=2)+'\n');print(json.dumps(pointer,ensure_ascii=False))
