#!/usr/bin/env python3
"""Regression test for the shared regional runner; uses only a temporary region."""
from __future__ import annotations
import hashlib,json,os,subprocess,sys,tempfile
from pathlib import Path
RUNNER=Path(__file__).with_name("runner.py")
NORMALIZER=RUNNER.with_name("normalize_layer.py")
sys.path.insert(0,str(RUNNER.parent.parent))
import manage
def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value)+"\n" if not isinstance(value,str) else value,encoding="utf-8")
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def run(region,*args,ok=True):
    result=subprocess.run([sys.executable,str(RUNNER),"--region-root",str(region),*args],capture_output=True,text=True)
    if (result.returncode==0)!=ok: raise AssertionError(result.stdout+result.stderr)
def main():
    core=RUNNER.parent.parent
    map_js=(core/"core"/"map.js").read_text(encoding="utf-8")
    assert "L.canvas(" in map_js,"large polygon layers must use Canvas"
    assert "animate:false" in map_js,"table navigation must avoid animated compositor races"
    assert "map.invalidateSize(" not in map_js,"overlay panels must not resize the map viewport"
    with tempfile.TemporaryDirectory(prefix="analytical-factory-test-") as temporary:
        region=Path(temporary)/"region";pipeline=region/"pipeline";outputs=pipeline/"outputs";outputs.mkdir(parents=True)
        source=region/"sources"/"settlements.csv"
        source.parent.mkdir();source.write_text("name,district,population,population_quality,latitude,longitude\nТест,Район,5000,official,55.0,60.0\nТест,Район,5000,official,55.0,60.0\n",encoding="utf-8")
        adapter={"source":"sources/settlements.csv","output":"settlements.geojson","property_map":{"name":"name","district":"district","population":"population","population_quality":"population_quality"},"numeric_fields":["population"],"required_fields":["name","population","population_quality"],"deduplicate_by":["name","district"],"geometry_types":["Point"]}
        write(region/"adapter.json",adapter)
        environment=os.environ|{"ANALYTICAL_REGION_ROOT":str(region),"ANALYTICAL_OUTPUT_DIR":str(outputs)}
        subprocess.run([sys.executable,str(NORMALIZER),"--config","adapter.json"],env=environment,check=True,capture_output=True,text=True)
        normalized=json.loads((outputs/"settlements.geojson").read_text());assert len(normalized["features"])==1;assert normalized["features"][0]["properties"]["population"]==5000
        write(outputs/"result.geojson",{"type":"FeatureCollection","features":[]})
        builder=region/"build.py";write(builder,"from pathlib import Path\nimport os\nPath(os.environ['ANALYTICAL_OUTPUT_DIR'],'result.geojson').write_text('{\"type\":\"FeatureCollection\",\"features\":[]}')\n")
        config={"schema_version":1,"region_id":region.name,"inputs":["build.py"],"outputs":["pipeline/outputs/result.geojson"],"steps":[{"name":"build result","command":["{python}","build.py"]}]}
        write(pipeline/"pipeline.json",config);run(region,"--write-lock");run(region,"--full")
        manifest=pipeline/"build_manifest.json";before=sha(manifest);run(region,"--full","--validate-only")
        assert sha(manifest)==before,"validate-only changed manifest"
        pristine=(outputs/"result.geojson").read_bytes()
        noop=region/"noop.py";write(noop,"pass\n");config["steps"]=[{"name":"forgot output","command":["{python}","noop.py"]}];config["inputs"]=["noop.py"]
        write(pipeline/"pipeline.json",config);run(region,"--write-lock");run(region,"--full",ok=False)
        assert (outputs/"result.geojson").read_bytes()==pristine,"failed clean full build changed prior outputs"
        (outputs/"result.geojson").write_text('{"type":"FeatureCollection","features":[{}]}\n',encoding="utf-8")
        run(region,"--full","--validate-only",ok=False)
        (outputs/"result.geojson").write_bytes(pristine)
        published=sha(outputs/"result.geojson");fail=region/"fail.py"
        write(fail,"from pathlib import Path\nimport os\nPath(os.environ['ANALYTICAL_OUTPUT_DIR'],'result.geojson').write_text('broken')\nraise SystemExit(7)\n")
        config["steps"]=[{"name":"intentional failure","command":["{python}","fail.py"]}];config["inputs"]=["fail.py"]
        write(pipeline/"pipeline.json",config);run(region,"--write-lock");run(region,"--full",ok=False)
        assert sha(outputs/"result.geojson")==published,"failed build changed published output"
        config["outputs"]=["pipeline/outputs/../../escape.geojson"];write(pipeline/"pipeline.json",config);run(region,"--write-lock",ok=False)
        adapter_region=Path(temporary)/"adapter-region";adapter_pipeline=adapter_region/"pipeline";adapter_pipeline.mkdir(parents=True)
        adapter_source=adapter_region/"sources"/"settlements.csv";adapter_source.parent.mkdir()
        adapter_source.write_text("name,district,population,population_quality,latitude,longitude\nОдин,Район,5000,official,55,60\n",encoding="utf-8")
        write(adapter_region/"adapter.json",adapter)
        adapter_pipeline_config={"schema_version":1,"region_id":"adapter-region","inputs":[],"outputs":["pipeline/outputs/settlements.geojson"],"steps":[{"name":"normalize","command":["{python}",str(NORMALIZER),"--config","adapter.json"]}]}
        write(adapter_pipeline/"pipeline.json",adapter_pipeline_config);run(adapter_region,"--write-lock");run(adapter_region,"--full");run(adapter_region,"--full","--validate-only")
        locked=json.loads((adapter_pipeline/"inputs.lock.json").read_text())["inputs"]
        assert "adapter.json" in locked and "sources/settlements.csv" in locked,"normalizer dependencies were not locked"
        adapter_source.write_text(adapter_source.read_text()+"Два,Район,6000,official,56,61\n",encoding="utf-8")
        run(adapter_region,"--full","--validate-only",ok=False)
    print("Shared pipeline self-test passed")
if __name__=="__main__": main()
