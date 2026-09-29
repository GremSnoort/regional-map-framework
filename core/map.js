import * as L from 'leaflet';
import {maplibreGL} from 'https://unpkg.com/@maplibre/maplibre-gl-leaflet@0.1.4/dist/leaflet-maplibre-gl.mjs';

const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const safeUrl=value=>{try{const url=new URL(String(value),location.href);return ['http:','https:'].includes(url.protocol)?url.href:null}catch{return null}};
const valueAt=(properties,path)=>String(path||'name').split('.').reduce((value,key)=>value?.[key],properties);
const numeric=(properties,path,fallback=0)=>{const value=Number(valueAt(properties,path));return Number.isFinite(value)?value:fallback};
const format=(value,type)=>type==='number'?Number(value||0).toLocaleString('ru-RU'):type==='decimal'?Number(value||0).toLocaleString('ru-RU',{maximumFractionDigits:2}):String(value??'');
const params=new URLSearchParams(location.search);
const registry=await fetch('registry.json').then(response=>response.ok?response.json():Promise.reject(new Error(`registry.json: HTTP ${response.status}`)));
const regionId=params.get('region')||registry.default_region;
if(!regionId){document.getElementById('title').innerHTML='<h1>Региональная аналитическая карта</h1><p>Каркас установлен, но регионы ещё не добавлены.</p>';document.getElementById('status').innerHTML='<b>Следующий шаг</b><br><span class="note">Создайте регион командой <code>python3 manage.py init-region</code>.</span>';throw new Error('No regions configured')}
if(!/^[a-z0-9_-]+$/.test(regionId)||!registry.regions.includes(regionId))throw new Error(`Неизвестный регион: ${regionId}`);
const base=`regions/${regionId}`;
const config=await fetch(`${base}/region.json`).then(response=>response.ok?response.json():Promise.reject(new Error(`region.json: HTTP ${response.status}`)));
document.title=config.page_title;
document.getElementById('title').innerHTML=`<h1>${esc(config.title)}</h1>${config.lifecycle==='draft'?'<p class="bad"><b>Черновик:</b> пакет ещё не прошёл production-проверку.</p>':''}<p>${esc(config.subtitle||'')}</p>`;
document.getElementById('sources').textContent=config.source_note||'Источники не описаны.';
fetch('/api/session',{cache:'no-store'}).then(response=>response.ok?response.json():null).then(session=>{if(session)document.getElementById('session-user').textContent=session.username});
const regeneration=document.getElementById('regeneration');
const regenerationUrl=`/api/regeneration?region=${encodeURIComponent(regionId)}`;
const regenerationTime=value=>value?new Date(value).toLocaleString('ru-RU'):'ещё не выполнялась';
let regenerationRequested=false;
async function refreshRegeneration(){try{const response=await fetch(regenerationUrl,{cache:'no-store'});if(!response.ok)return;const state=await response.json();if(!state.enabled)return;if(regenerationRequested&&state.status==='succeeded'){location.reload();return}if(state.status==='failed')regenerationRequested=false;regeneration.hidden=false;const running=state.status==='running';regeneration.innerHTML=`<b>Перегенерировать публичные данные</b><br><span>Последняя успешная генерация: ${esc(regenerationTime(state.last_success_at))}</span>${state.status==='failed'?`<br><span class="bad">Ошибка: ${esc(state.error||'неизвестно')}</span>`:''}<br><button type="button" ${running?'disabled':''}>${running?'Генерация выполняется…':'Перегенерировать'}</button><br><small>Не чаще одного раза в ${Math.ceil(Number(state.min_interval_seconds||300)/60)} мин.</small>`;regeneration.querySelector('button')?.addEventListener('click',async()=>{const button=regeneration.querySelector('button');button.disabled=true;const result=await fetch(regenerationUrl,{method:'POST'});if(result.status===429){const body=await result.json();alert(`Повторный запуск будет доступен через ${body.retry_after_seconds} сек.`)}else if(!result.ok){const body=await result.json().catch(()=>({}));alert(body.error||`HTTP ${result.status}`)}else regenerationRequested=true;await refreshRegeneration()});if(running)setTimeout(refreshRegeneration,3000)}catch{regeneration.hidden=true}}
refreshRegeneration();

const map=L.map('map',{zoomControl:false,scrollWheelZoom:true,doubleClickZoom:true,touchZoom:true,boxZoom:true,keyboard:true,zoomSnap:.25,zoomDelta:.5,minZoom:config.view.min_zoom,maxZoom:config.view.max_zoom}).setView(config.view.center,config.view.zoom);
L.control.zoom({position:'bottomleft',zoomInTitle:'Приблизить',zoomOutTitle:'Отдалить'}).addTo(map);
const basemap=maplibreGL({style:'https://tiles.openfreemap.org/styles/positron'}).addTo(map);
const vectorCanvas=L.canvas({pane:'overlayPane',padding:.35,tolerance:3});
const overlays={},status=[],runtime=[];
const palette=['#2563eb','#dc2626','#16a34a','#9333ea','#ea580c','#0891b2','#be123c','#4f46e5'];
const load=async spec=>{const response=await fetch(`${base}/${spec.file}`);if(!response.ok)throw new Error(`${spec.file}: HTTP ${response.status}`);return response.json()};
const binFor=(spec,value)=>Array.isArray(spec.bins)?spec.bins.find(bin=>bin.max===null||value<Number(bin.max)):null;
const categoryFor=(spec,properties)=>spec.categories?.[String(valueAt(properties,spec.category_field)??'')];
const popupHtml=(properties,spec)=>{
  const fields=spec.popup_fields||Object.keys(properties).slice(0,12);
  return fields.map(field=>{const definition=typeof field==='string'?{field,label:field}:field,value=valueAt(properties,definition.field);if(value===undefined||value===null||value==='')return'';const url=definition.type==='url'?safeUrl(value):null;const rendered=definition.type==='url'?(url?`<a href="${esc(url)}" target="_blank" rel="noopener">${esc(definition.link_label||value)}</a>`:esc(value)):esc(format(value,definition.type));return `<b>${esc(definition.label||definition.field)}:</b> ${rendered}${esc(definition.suffix||'')}`}).filter(Boolean).join('<br>');
};
const externalMapSettings=spec=>spec.external_map_link&&typeof spec.external_map_link==='object'?spec.external_map_link:null;
const externalMapLink=(feature,item,spec)=>{
  const settings=externalMapSettings(spec);if(!settings)return'';
  const bounds=typeof item.getBounds==='function'?item.getBounds():null;
  const center=bounds?.isValid?.()?bounds.getCenter():typeof item.getLatLng==='function'?item.getLatLng():null;
  if(!center||!Number.isFinite(center.lat)||!Number.isFinite(center.lng))return'';
  if(settings.related_layer){
    const related=runtime.find(state=>state.id===settings.related_layer);if(!related)return'';
    const candidates=related.data.features||[],joinValue=valueAt(feature.properties||{},settings.feature_join_field);
    const matches=joinValue===undefined||joinValue===null?[]:candidates.filter(candidate=>String(valueAt(candidate.properties||{},settings.related_join_field)||'').toLocaleLowerCase('ru')===String(joinValue).toLocaleLowerCase('ru'));
    if(!matches.length)return'';
    if(settings.related_filter_field&&!matches.some(candidate=>numeric(candidate.properties||{},settings.related_filter_field,NaN)>Number(settings.related_filter_min_exclusive??0)))return'';
  }
  const grid=Number(feature.properties?.grid_metres||0),zoom=Number(settings.zoom??(grid&&grid<=250?17:grid&&grid<=500?16:15)),ll=`${center.lng.toFixed(6)},${center.lat.toFixed(6)}`;
  const url=new URL('https://yandex.ru/maps/');url.searchParams.set('ll',ll);url.searchParams.set('z',String(zoom));url.searchParams.set('pt',`${ll},pm2rdm`);
  return `<a class="popup-map-link" href="${esc(url.href)}" target="_blank" rel="noopener">${esc(settings.label||'Открыть в Яндекс Картах')}</a>`;
};
const featureTitle=(feature,spec)=>valueAt(feature.properties||{},spec.title_field)||feature.properties?.name||feature.properties?.address||spec.label;
const featureStyle=(feature,spec,index)=>{
  const properties=feature.properties||{},style=spec.style||{},value=numeric(properties,spec.value_field,NaN),bin=Number.isFinite(value)?binFor(spec,value):null,category=categoryFor(spec,properties),color=category?.color||bin?.color||style.color||palette[index%palette.length];
  if(['LineString','MultiLineString'].includes(feature.geometry?.type))return{color,weight:Number(style.weight??3),opacity:Number(style.opacity??.85),dashArray:style.dash_array||null,lineCap:'round',lineJoin:'round'};
  const fillVisible=style.fill_max_zoom===undefined||map.getZoom()<=Number(style.fill_max_zoom);
  return{color:style.stroke||'#374151',weight:Number(style.weight??.7),opacity:Number(style.opacity??.9),fillColor:color,fillOpacity:fillVisible?Number(style.fill_opacity??.55):Number(style.fill_opacity_above_max??0)};
};
const pointLayer=(feature,latlng,spec,index)=>{
  const properties=feature.properties||{},style=spec.style||{},value=numeric(properties,spec.value_field,NaN),bin=Number.isFinite(value)?binFor(spec,value):null,category=categoryFor(spec,properties),color=category?.color||bin?.color||style.color||palette[index%palette.length];
  if(spec.marker==='icon')return L.marker(latlng,{title:featureTitle(feature,spec)});
  const scale=spec.size_field?numeric(properties,spec.size_field):0,radius=Math.max(Number(style.min_radius??3),Math.min(Number(style.max_radius??18),Number(style.radius??6)+scale*Number(style.radius_scale??1)));
  return L.circleMarker(latlng,{radius,color:style.stroke||color,weight:Number(style.weight??1.2),fillColor:color,fillOpacity:Number(style.fill_opacity??.82)});
};

const specs=Object.entries(config.layers||{}).sort((a,b)=>Number(a[1].z_index??a[1].order??0)-Number(b[1].z_index??b[1].order??0));
for(let index=0;index<specs.length;index++){
  const [id,spec]=specs[index];
  try{
    const data=await load(spec);
    const layer=L.geoJSON(data,{renderer:vectorCanvas,interactive:spec.interactive!==false,style:feature=>({...featureStyle(feature,spec,index),pane:'overlayPane',renderer:vectorCanvas}),pointToLayer:(feature,latlng)=>{const point=pointLayer(feature,latlng,spec,index);point.options.pane='overlayPane';point.options.renderer=vectorCanvas;return point},onEachFeature:(feature,item)=>{const properties=feature.properties||{},title=featureTitle(feature,spec),body=popupHtml(properties,spec),label=valueAt(properties,spec.label_field)||title,permanent=Boolean(spec.label_field)&&(!spec.label_min_field||numeric(properties,spec.label_min_field)>=Number(spec.label_min_value??0));item.bindTooltip(esc(label),permanent?{permanent:true,interactive:true,direction:spec.label_direction||'right',className:'feature-label'}:{sticky:true});if(permanent)item.getTooltip()?.on('click',()=>item.openPopup());if(body||externalMapSettings(spec))item.bindPopup(()=>{const link=externalMapLink(feature,item,spec);return `<b>${esc(title)}</b>${body?`<br>${body}`:''}${link?`<br>${link}`:''}`})}});
    const state={id,spec,data,layer,index,suppressed:spec.default_visible===false};runtime.push(state);overlays[spec.label]=layer;
    if(!state.suppressed&&(!spec.min_zoom||map.getZoom()>=spec.min_zoom)&&(!spec.max_zoom||map.getZoom()<=spec.max_zoom))layer.addTo(map);
    if(spec.fit_bounds&&layer.getBounds().isValid())map.fitBounds(layer.getBounds(),{padding:[15,15],animate:false});
    status.push(`<span class="ok">${esc(spec.label)}: ${(data.features||[]).length}</span>`);
  }catch(error){status.push(`<span class="bad">${esc(spec.label)}: ${esc(error.message)}</span>`)}
}

const inZoomRange=state=>(!state.spec.min_zoom||map.getZoom()>=state.spec.min_zoom)&&(!state.spec.max_zoom||map.getZoom()<=state.spec.max_zoom);
function syncZoomLayers(){for(const state of runtime){const visible=inZoomRange(state);if(visible&&!state.suppressed&&!map.hasLayer(state.layer))state.layer.addTo(map);if(!visible&&map.hasLayer(state.layer))map.removeLayer(state.layer);if(visible&&state.layer.setStyle)state.layer.setStyle(feature=>({...featureStyle(feature,state.spec,state.index),pane:'overlayPane',renderer:vectorCanvas}))}for(const state of runtime){if(map.hasLayer(state.layer)&&state.layer.bringToFront)state.layer.bringToFront()}}
map.on('zoomend',syncZoomLayers);
map.on('overlayremove',event=>{const state=runtime.find(item=>item.layer===event.layer);if(state&&inZoomRange(state))state.suppressed=true});
map.on('overlayadd',event=>{const state=runtime.find(item=>item.layer===event.layer);if(state){state.suppressed=false;syncZoomLayers()}});
L.control.layers({'OpenFreeMap · Positron':basemap},overlays,{collapsed:false,position:'topright'}).addTo(map);syncZoomLayers();

function relatedBounds(feature,spec){
  const table=spec.table||{},joinValue=valueAt(feature.properties||{},table.related_value_field||table.join_field||'name');
  if(!table.related_layer||joinValue===undefined)return null;
  const related=runtime.find(item=>item.id===table.related_layer);if(!related)return null;
  const field=table.related_join_field||'name',features=(related.data.features||[]).filter(item=>String(valueAt(item.properties||{},field)).toLocaleLowerCase()===String(joinValue).toLocaleLowerCase());
  if(!features.length)return null;const bounds=L.geoJSON({type:'FeatureCollection',features}).getBounds();return bounds.isValid()?bounds:null;
}
function focusFeature(feature,state){
  const bounds=relatedBounds(feature,state.spec)||L.geoJSON(feature).getBounds();
  if(bounds.isValid())map.fitBounds(bounds,{padding:[35,35],maxZoom:Number(state.spec.table?.max_zoom||16),animate:false});
  setTimeout(()=>state.layer.eachLayer(layer=>{if(layer.feature===feature)layer.openPopup()}),80);
}
function buildTable(state,tableIndex){
  const options=state.spec.table;if(!options?.enabled)return;
  let features=[...(state.data.features||[])];
  if(options.filter_field)features=features.filter(feature=>numeric(feature.properties||{},options.filter_field)>Number(options.filter_min??0));
  const sorts=options.sort||[];features.sort((a,b)=>{for(const rule of sorts){const av=valueAt(a.properties||{},rule.field),bv=valueAt(b.properties||{},rule.field),direction=rule.direction==='asc'?1:-1,difference=typeof av==='number'&&typeof bv==='number'?(av-bv):String(av??'').localeCompare(String(bv??''),'ru');if(difference)return difference*direction}return 0});
  const panel=document.createElement('aside');panel.className='data-panel';panel.style.top=`${180+tableIndex*46}px`;panel.innerHTML=`<button class="data-panel-toggle" type="button" aria-expanded="false"><span>☰ ${esc(options.button_label||state.spec.label)}</span><b>${features.length}</b></button><div class="data-panel-content"><header><div><h2>${esc(options.title||state.spec.label)}</h2><p>${esc(options.subtitle||'Объекты в заданном порядке')}</p></div><button class="data-panel-close" type="button" aria-label="Свернуть">×</button></header><div class="data-table-wrap"><table><thead><tr><th>№</th>${(options.columns||[]).map(column=>`<th>${esc(column.label||column.field)}</th>`).join('')}</tr></thead><tbody></tbody></table></div><footer>${esc(options.footer||'Нажмите на строку для перехода к объекту.')}</footer></div>`;
  document.body.appendChild(panel);L.DomEvent.disableClickPropagation(panel);L.DomEvent.disableScrollPropagation(panel);
  const closedTop=`${180+tableIndex*46}px`,toggle=panel.querySelector('.data-panel-toggle'),close=panel.querySelector('.data-panel-close'),body=panel.querySelector('tbody'),setOpen=open=>{if(open)document.querySelectorAll('.data-panel.open').forEach(other=>{if(other!==panel){other.classList.remove('open');other.style.top=other.dataset.closedTop;other.querySelector('.data-panel-toggle')?.setAttribute('aria-expanded','false')}});panel.classList.toggle('open',open);panel.style.top=open?'16px':closedTop;toggle.setAttribute('aria-expanded',String(open));if(open)map.closePopup()};panel.dataset.closedTop=closedTop;
  toggle.addEventListener('click',()=>setOpen(!panel.classList.contains('open')));close.addEventListener('click',()=>setOpen(false));
  features.forEach((feature,index)=>{const row=document.createElement('tr');row.tabIndex=0;row.setAttribute('role','button');row.innerHTML=`<td>${index+1}</td>${(options.columns||[]).map(column=>`<td>${column.bold?'<b>':''}${esc(format(valueAt(feature.properties||{},column.field),column.type))}${esc(column.suffix||'')}${column.bold?'</b>':''}</td>`).join('')}`;const focus=()=>{setOpen(false);focusFeature(feature,state)};row.addEventListener('click',focus);row.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();focus()}});body.appendChild(row)});
}
runtime.filter(state=>state.spec.table?.enabled).forEach(buildTable);

const densityLegendSignature=spec=>spec.renderer==='density'&&Array.isArray(spec.bins)&&spec.bins.length
  ?JSON.stringify([spec.value_field||'',spec.bins.map(bin=>[bin.max,bin.label,bin.color])])
  :null;
const densityLegendTitle=(states,spec)=>{
  if(states.length===1)return spec.legend_title||spec.label;
  const titles=new Set(states.map(state=>state.spec.legend_title).filter(Boolean));
  if(titles.size===1)return [...titles][0];
  const resolutions=new Set(states.map(state=>String(state.spec.label||'').match(/(?:,|\s)(\d+(?:[.,]\d+)?)\s*м\b/i)?.[1]).filter(Boolean));
  const suffix=resolutions.size===1?`, ${[...resolutions][0]} м`:'';
  return `Общая шкала моделей плотности${suffix}`;
};
let legendElement=null;
function renderLegend(){
  if(!legendElement)return;
  let html='';
  const visible=runtime.filter(state=>map.hasLayer(state.layer));
  const seenDensity=new Set();
  for(const state of visible){
    const spec=state.spec;
    if(spec.legend===false)continue;
    const signature=densityLegendSignature(spec);
    if(signature&&seenDensity.has(signature))continue;
    if(signature)seenDensity.add(signature);
    const title=signature?densityLegendTitle(visible.filter(other=>densityLegendSignature(other.spec)===signature),spec):(spec.legend_title||spec.label);
    if(spec.categories&&Object.keys(spec.categories).length){
      html+=`<section><b>${esc(title)}</b><br>${Object.entries(spec.categories).map(([value,item])=>`<i style="background:${esc(item.color)}"></i>${esc(item.label||value)}<br>`).join('')}</section>`;
    }else if(Array.isArray(spec.bins)&&spec.bins.length){
      html+=`<section><b>${esc(title)}</b><br>${spec.bins.map(bin=>`<i style="background:${esc(bin.color)}"></i>${esc(bin.label??(bin.max===null?'и выше':`< ${bin.max}`))}<br>`).join('')}</section>`;
    }else if(spec.legend_symbol!==false){const color=spec.style?.color||palette[state.index%palette.length];html+=`<div><i class="symbol" style="background:${esc(color)}"></i>${esc(spec.label)}</div>`}
  }
  legendElement.innerHTML=html||'<small>Нет активных слоёв с легендой.</small>';
}
const legend=L.control({position:'bottomright'});
legend.onAdd=()=>{
  legendElement=L.DomUtil.create('div','legend');
  L.DomEvent.disableClickPropagation(legendElement);
  L.DomEvent.disableScrollPropagation(legendElement);
  renderLegend();
  return legendElement;
};
legend.addTo(map);
map.on('zoomend overlayadd overlayremove',renderLegend);
L.control.scale({imperial:false,maxWidth:180}).addTo(map);
document.getElementById('status').innerHTML=`<b>Слои региона</b><br>${status.join('<br>')||'<span class="note">Слои ещё не настроены.</span>'}<br><span class="note">${esc(config.status_note||'Аналитические результаты требуют проверки методики и источников.')}</span>`;
if(params.has('lat')&&params.has('lon')&&params.has('zoom')){const lat=Number(params.get('lat')),lon=Number(params.get('lon')),zoom=Number(params.get('zoom'));if([lat,lon,zoom].every(Number.isFinite))map.setView([lat,lon],zoom,{animate:false})}
