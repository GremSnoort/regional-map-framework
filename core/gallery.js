const params=new URLSearchParams(location.search);
const legacyRegion=params.get('region');

if(legacyRegion){
  location.replace('/map.html?'+params.toString());
}else{
  await renderGallery();
}

async function renderGallery(){
  const gallery=document.getElementById('region-gallery');
  const status=document.getElementById('gallery-status');
  const count=document.getElementById('region-count');

  fetch('/api/session',{cache:'no-store'})
    .then(response=>response.ok?response.json():null)
    .then(session=>{if(session)document.getElementById('session-user').textContent=session.username})
    .catch(()=>{});

  try{
    const response=await fetch('/registry.json',{cache:'no-store'});
    if(!response.ok)throw new Error('registry.json: HTTP '+response.status);
    const registry=await response.json();
    const ids=Array.isArray(registry.regions)?registry.regions:[];
    if(!ids.length){
      status.textContent='Регионы ещё не добавлены. Создайте и зарегистрируйте первый региональный пакет.';
      status.classList.add('empty');
      count.textContent='0 регионов';
      return;
    }

    const results=await Promise.all(ids.map(async(id,index)=>{
      try{
        const item=await fetch('/regions/'+encodeURIComponent(id)+'/region.json',{cache:'no-store'});
        if(!item.ok)throw new Error('HTTP '+item.status);
        return{id,index,config:await item.json()};
      }catch(error){return{id,index,error}}
    }));

    status.hidden=true;
    const available=results.filter(item=>item.config);
    count.textContent=plural(available.length,'регион','региона','регионов');
    for(const item of results)gallery.appendChild(item.config?regionCard(item):errorCard(item));
  }catch(error){
    status.textContent='Не удалось загрузить галерею: '+error.message;
    status.classList.add('error');
  }
}

function regionCard({id,index,config}){
  const link=document.createElement('a');
  link.className='region-card';
  link.href='/map.html?region='+encodeURIComponent(id);
  link.style.setProperty('--hue',String((index*53+132)%360));

  const visual=document.createElement('div');
  visual.className='card-visual';
  visual.setAttribute('aria-hidden','true');
  const initials=document.createElement('span');
  initials.textContent=initialsFor(config.title||id);
  visual.appendChild(initials);

  const body=document.createElement('div');
  body.className='card-body';
  const meta=document.createElement('div');
  meta.className='card-meta';
  const badge=document.createElement('span');
  const production=config.lifecycle==='production';
  badge.className='badge '+(production?'production':'draft');
  badge.textContent=production?'Готово':'Черновик';
  const layers=document.createElement('span');
  const layerCount=config.layers&&typeof config.layers==='object'?Object.keys(config.layers).length:0;
  layers.textContent=plural(layerCount,'слой','слоя','слоёв');
  meta.append(badge,layers);

  const title=document.createElement('h3');
  title.textContent=config.title||id;
  const subtitle=document.createElement('p');
  subtitle.textContent=config.subtitle||'Интерактивная региональная аналитическая карта.';
  const action=document.createElement('span');
  action.className='card-action';
  action.textContent='Открыть карту';
  body.append(meta,title,subtitle,action);
  link.append(visual,body);
  return link;
}

function errorCard({id,error}){
  const card=document.createElement('article');
  card.className='region-card broken';
  const body=document.createElement('div');
  body.className='card-body';
  const title=document.createElement('h3');
  title.textContent=id;
  const message=document.createElement('p');
  message.textContent='Конфигурация региона недоступна: '+error.message;
  body.append(title,message);
  card.append(body);
  return card;
}

function initialsFor(value){
  return String(value).trim().split(/\s+/).slice(0,2).map(word=>word[0]||'').join('').toLocaleUpperCase('ru');
}

function plural(number,one,few,many){
  const mod100=number%100,mod10=number%10;
  const word=mod100>=11&&mod100<=14?many:mod10===1?one:mod10>=2&&mod10<=4?few:many;
  return number.toLocaleString('ru-RU')+' '+word;
}
