"""Create a private, standalone review map from a validated inventory sample."""
import argparse
import json
from pathlib import Path
from verify_housing_v3 import verify_v3

TEMPLATE='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Приморский край — проверка жилья v3</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{margin:0;font:15px system-ui;color:#18232b}header{padding:12px 18px;background:#f5f7f9}h1{font-size:20px;margin:0 0 8px}p{margin:6px 0}select{max-width:95%;margin:5px 12px 5px 0;padding:7px}#map{height:calc(100vh - 190px);min-height:400px}.leaflet-popup-content{max-width:360px}dt{font-weight:600;margin-top:7px}dd{margin:0;overflow-wrap:anywhere}</style>
<header><h1>Приморский край: проверка жилого фонда</h1><p>Выборка: 1 000 приоритетных контуров из 411 886. Кадастровая привязка и фактическая заселённость не подтверждены. Изменения к плотности не применены.</p><label>Область <select id="town"><option value="">Все</option></select></label><label>Проверка <select id="status"><option value="">Все статусы</option></select></label><span id="count"></span><p>Красный — конфликт / неоднозначность; оранжевый — адресный алиас; синий — адресный кандидат; серый — нет сопоставления.</p></header><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const data=__DATA__;
const map=L.map('map').setView([44.5,134],7);L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap contributors',maxZoom:19}).addTo(map);
const towns=document.querySelector('#town'),statuses=document.querySelector('#status');
for(const value of [...new Set(data.features.map(f=>f.properties.settlement))].sort()){const o=document.createElement('option');o.value=value;o.textContent=value;towns.append(o)}
const labels=new Map(data.features.map(f=>[f.properties.match_status,f.properties.match_status_label]));for(const [key,label] of labels){const o=document.createElement('option');o.value=key;o.textContent=label;statuses.append(o)}
let layer;function render(){if(layer)map.removeLayer(layer);const selected=data.features.filter(f=>(!towns.value||f.properties.settlement===towns.value)&&(!statuses.value||f.properties.match_status===statuses.value));layer=L.geoJSON(selected,{style:f=>({color:['quarantined','ambiguous'].includes(f.properties.match_status)?'#bc2424':f.properties.match_status==='alias_review'?'#c16a04':f.properties.match_status==='unmatched'?'#75818a':'#1974a5',weight:2,fillOpacity:.4}),onEachFeature:(f,l)=>{const p=f.properties,box=document.createElement('dl');for(const [label,key] of [['OSM','osm_id'],['Адрес на контуре','address'],['Область модели','settlement'],['Результат','match_status_label'],['Официальные адреса-кандидаты','official_addresses'],['Адресные объекты OSM','source_address_evidence'],['Исключённые прежние привязки','removed_links'],['Этажность из источников','reported_storeys'],['Статус этажности','storeys_status'],['Заселённость','occupancy_note'],['Привязка','geometry_note'],['Расселение','resettlement_note']]){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=p[key]??'Неизвестно';box.append(dt,dd)}for(const [label,key] of [['Источник','source_url'],['Программа расселения','resettlement_source_url'],['OSM','osm_url']]){if(p[key]){const a=document.createElement('a');a.href=p[key];a.textContent=label;a.target='_blank';a.rel='noopener';box.append(a,document.createElement('br'))}}l.bindPopup(box)}}).addTo(map);document.querySelector('#count').textContent=`Показано: ${selected.length}`;if(selected.length)map.fitBounds(layer.getBounds(),{padding:[20,20],maxZoom:16})}towns.onchange=render;statuses.onchange=render;render();
</script></html>'''

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--dataset',type=Path,required=True);args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    verify_v3(root,args.dataset);data=json.loads((args.dataset/'housing_quality.geojson').read_text())
    encoded=json.dumps(data,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    output=root/'review/housing-corrected-v3-map.html';output.write_text(TEMPLATE.replace('__DATA__',encoded));print(str(output))
