const params=new URLSearchParams(location.search);
const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const validUrl=value=>{try{const url=new URL(String(value));return ['http:','https:'].includes(url.protocol)?url.href:null}catch{return null}};
const regionId=params.get('region');
const kindLabels={agency:'Агентство',realtor:'Риэлтор',broker:'Брокер',developer:'Девелопер',property_manager:'Управляющая компания',other:'Другое'};
let published={contacts:[]},session=null,pipeline=null;

if(!regionId||!/^[a-z0-9_-]+$/.test(regionId))failPage('Регион не указан или имеет неверный идентификатор.');
else await initialize();

async function initialize(){
  try{
    const [registryResponse,sessionResponse]=await Promise.all([fetch('/registry.json',{cache:'no-store'}),fetch('/api/session',{cache:'no-store'})]);
    if(!registryResponse.ok)throw new Error('Не удалось загрузить реестр регионов');
    const registry=await registryResponse.json();
    if(!registry.regions?.includes(regionId))throw new Error('Регион отсутствует в реестре');
    session=sessionResponse.ok?await sessionResponse.json():null;
    if(session)document.getElementById('session-user').textContent=session.username;
    const configResponse=await fetch(`/regions/${encodeURIComponent(regionId)}/region.json`,{cache:'no-store'});
    if(!configResponse.ok)throw new Error('Конфигурация региона недоступна');
    const config=await configResponse.json();
    document.title=`Контакты — ${config.title}`;
    document.getElementById('page-title').textContent=config.title;
    document.getElementById('map-link').href=`/map.html?region=${encodeURIComponent(regionId)}`;
    await loadCatalog();
    if(session?.is_admin){document.getElementById('admin-panel').hidden=false;await refreshPipeline()}
  }catch(error){failPage(error.message)}
}

async function loadCatalog(){
  const response=await fetch(`/api/contacts?region=${encodeURIComponent(regionId)}`,{cache:'no-store'});
  const status=document.getElementById('catalog-status');
  if(response.status===404){status.textContent='Для этого региона каталог контактов пока не подключён.';status.classList.add('empty');return}
  if(!response.ok)throw new Error(`Каталог контактов: HTTP ${response.status}`);
  published=await response.json();
  buildFilters();renderContacts();
  const date=published.published_at?new Date(published.published_at).toLocaleString('ru-RU'):'ещё не публиковался';
  document.getElementById('catalog-meta').textContent=`Последняя публикация: ${date}`;
}

function buildFilters(){
  const fill=(id,values)=>{const select=document.getElementById(id);for(const value of [...new Set(values.filter(Boolean))].sort((a,b)=>a.localeCompare(b,'ru'))){const option=document.createElement('option');option.value=value;option.textContent=value;select.appendChild(option)}select.addEventListener('change',renderContacts)};
  fill('kind-filter',published.contacts.map(item=>item.kind));
  [...document.getElementById('kind-filter').options].forEach(option=>{if(option.value)option.textContent=kindLabels[option.value]||option.value});
  fill('coverage-filter',published.contacts.flatMap(item=>item.coverage||[]));
  fill('specialization-filter',published.contacts.flatMap(item=>item.specializations||[]));
  document.getElementById('search').addEventListener('input',renderContacts);
}

function renderContacts(){
  const query=document.getElementById('search').value.trim().toLocaleLowerCase('ru');
  const kind=document.getElementById('kind-filter').value,coverage=document.getElementById('coverage-filter').value,specialization=document.getElementById('specialization-filter').value;
  const rows=published.contacts.filter(item=>{
    const haystack=[item.name,...item.coverage,...item.specializations,...item.phones,...item.emails,...item.addresses].join(' ').toLocaleLowerCase('ru');
    return(!query||haystack.includes(query))&&(!kind||item.kind===kind)&&(!coverage||item.coverage.includes(coverage))&&(!specialization||item.specializations.includes(specialization));
  });
  const list=document.getElementById('contact-list'),status=document.getElementById('catalog-status');list.replaceChildren();
  status.textContent=rows.length?`${rows.length.toLocaleString('ru-RU')} из ${published.contacts.length.toLocaleString('ru-RU')} записей`:'Ничего не найдено по заданным фильтрам.';status.className='catalog-status'+(rows.length?'':' empty');
  for(const item of rows)list.appendChild(contactCard(item));
}

function contactCard(item){
  const card=document.createElement('article');card.className='contact-card';
  const header=document.createElement('header');const heading=document.createElement('div');heading.innerHTML=`<span class="kind">${esc(kindLabels[item.kind]||item.kind)}</span><h3>${esc(item.name)}</h3>`;
  const coverage=document.createElement('p');coverage.className='coverage';coverage.textContent=(item.coverage||[]).join(' · ')||'Территория не указана';header.append(heading,coverage);card.appendChild(header);
  if(item.specializations?.length){const tags=document.createElement('div');tags.className='tags';item.specializations.forEach(value=>{const tag=document.createElement('span');tag.textContent=value;tags.appendChild(tag)});card.appendChild(tags)}
  const contacts=document.createElement('div');contacts.className='contact-methods';
  appendLinks(contacts,item.phones,value=>`tel:${value}`,value=>value);
  appendLinks(contacts,item.emails,value=>`mailto:${value}`,value=>value);
  appendLinks(contacts,item.websites,value=>validUrl(value),value=>{try{return new URL(value).hostname}catch{return value}});
  for(const link of item.links||[]){const url=validUrl(link.url);if(url)contacts.appendChild(anchor(url,link.label||link.type,true))}
  card.appendChild(contacts);
  if(item.addresses?.length){const addresses=document.createElement('p');addresses.className='addresses';addresses.textContent=item.addresses.join(' · ');card.appendChild(addresses)}
  const sources=document.createElement('details');const summary=document.createElement('summary');summary.textContent=`Источники: ${item.sources?.length||0}`;sources.appendChild(summary);
  const sourceList=document.createElement('ul');for(const source of item.sources||[]){const row=document.createElement('li');const date=source.retrieved_at?new Date(source.retrieved_at).toLocaleDateString('ru-RU'):'';const url=validUrl(source.url);if(url)row.append(anchor(url,source.source_id,true));else row.append(document.createTextNode(source.source_id));if(date)row.append(document.createTextNode(` · проверено ${date}`));sourceList.appendChild(row)}sources.appendChild(sourceList);card.appendChild(sources);
  return card;
}

function appendLinks(parent,values,urlFor,labelFor){for(const value of values||[]){const url=urlFor(value);if(url)parent.appendChild(anchor(url,labelFor(value),url.startsWith('http')))}}
function anchor(url,label,external=false){const item=document.createElement('a');item.href=url;item.textContent=label;if(external){item.target='_blank';item.rel='noopener'}return item}

async function refreshPipeline(){
  const response=await fetch(`/api/contacts/status?region=${encodeURIComponent(regionId)}`,{cache:'no-store'});
  if(!response.ok){document.getElementById('pipeline-state').textContent=response.status===404?'Конвейер для региона не настроен.':`Не удалось получить состояние: HTTP ${response.status}`;return}
  pipeline=await response.json();
  const running=pipeline.status==='running',failed=pipeline.status==='failed';
  document.getElementById('pipeline-state').textContent=running?'Сбор выполняется…':failed?`Последний сбор завершился ошибкой: ${pipeline.error||'неизвестная ошибка'}`:pipeline.last_success_at?`Последний сбор: ${new Date(pipeline.last_success_at).toLocaleString('ru-RU')}`:'Сбор ещё не запускался.';
  document.getElementById('pipeline-summary').innerHTML=summaryHtml(pipeline.comparison,pipeline.source_errors);
  document.getElementById('collect-button').disabled=running;
  document.getElementById('review-button').disabled=running||!pipeline.has_candidates;
  if(running)setTimeout(refreshPipeline,3000);
}

function summaryHtml(diff={},errors=[]){return `<span>Новых: <b>${diff.added?.length||0}</b></span><span>Изменено: <b>${diff.changed?.length||0}</b></span><span>Исчезло: <b>${diff.removed?.length||0}</b></span><span>Без изменений: <b>${diff.unchanged||0}</b></span>${errors?.length?`<span class="warning">Ошибок источников: <b>${errors.length}</b></span>`:''}`}

document.getElementById('collect-button').addEventListener('click',async()=>{
  if(!confirm('Запустить сбор публичных деловых контактов из настроенных источников? Опубликованный каталог не изменится.'))return;
  resetApproval();const response=await fetch(`/api/contacts/collect?region=${encodeURIComponent(regionId)}`,{method:'POST'});const body=await response.json().catch(()=>({}));
  if(response.status===429)alert(`Повторный сбор доступен через ${body.retry_after_seconds} сек.`);else if(!response.ok)alert(body.error||`HTTP ${response.status}`);await refreshPipeline();
});

document.getElementById('review-button').addEventListener('click',async()=>{
  const response=await fetch(`/api/contacts/candidates?region=${encodeURIComponent(regionId)}`,{cache:'no-store'});if(!response.ok){alert(`Не удалось загрузить кандидатов: HTTP ${response.status}`);return}
  const candidates=await response.json(),diff=pipeline?.comparison||{};const oldById=new Map(published.contacts.map(item=>[item.contact_id,item])),newById=new Map(candidates.contacts.map(item=>[item.contact_id,item]));
  const section=document.getElementById('review-results');section.hidden=false;section.replaceChildren(reviewGroup('Новые записи',diff.added,newById),reviewGroup('Изменённые записи',diff.changed,newById),reviewGroup('Записи, отсутствующие в новой сборке',diff.removed,oldById));
  const errors=document.createElement('div');errors.className='source-errors';errors.innerHTML=`<h3>Ошибки источников</h3>${candidates.source_errors?.length?`<ul>${candidates.source_errors.map(item=>`<li><b>${esc(item.source_id)}</b>: ${esc(item.error)}</li>`).join('')}</ul>`:'<p>Ошибок источников нет.</p>'}`;section.appendChild(errors);
  document.getElementById('approval-check').disabled=false;section.scrollIntoView({behavior:'smooth',block:'nearest'});
});

function reviewGroup(title,ids=[],lookup){const group=document.createElement('section');const heading=document.createElement('h3');heading.textContent=`${title}: ${ids.length}`;group.appendChild(heading);const list=document.createElement('ul');for(const id of ids){const row=document.createElement('li'),item=lookup.get(id);row.textContent=item?`${item.name} — ${item.phones?.join(', ')||item.emails?.join(', ')||id}`:id;list.appendChild(row)}if(!ids.length){const empty=document.createElement('p');empty.textContent='Нет.';group.appendChild(empty)}else group.appendChild(list);return group}

document.getElementById('approval-check').addEventListener('change',event=>{document.getElementById('publish-button').disabled=!event.target.checked});
document.getElementById('publish-button').addEventListener('click',async()=>{
  if(!document.getElementById('approval-check').checked||!confirm('Опубликовать проверенный набор? Текущий каталог будет заменён атомарно.'))return;
  const response=await fetch(`/api/contacts/publish?region=${encodeURIComponent(regionId)}`,{method:'POST'}),body=await response.json().catch(()=>({}));if(!response.ok){alert(body.error||`HTTP ${response.status}`);return}location.reload();
});
function resetApproval(){const check=document.getElementById('approval-check');check.checked=false;check.disabled=true;document.getElementById('publish-button').disabled=true;document.getElementById('review-results').hidden=true}
function failPage(message){document.getElementById('catalog-status').textContent=message;document.getElementById('catalog-status').className='catalog-status error'}
