"""Typed address identities; spatial context never confirms a physical house."""
import re
from housing_inventory import token

STREET_TYPES={
    'улица':'street','ул':'street','переулок':'lane','пер':'lane',
    'проспект':'avenue','пр-кт':'avenue','пр-т':'avenue',
    'квартал':'quarter','кв-л':'quarter','микрорайон':'microdistrict','мкр':'microdistrict',
    'бульвар':'boulevard','бул':'boulevard','шоссе':'highway','ш':'highway',
    'проезд':'drive','пр-д':'drive','тупик':'dead_end','площадь':'square','пл':'square',
    'линия':'line','территория':'territory','тер':'territory','аллея':'alley',
    'набережная':'embankment','наб':'embankment',
}
LOCALITY_TYPES=('город','г','посёлок городского типа','поселок городского типа','пгт',
                'рабочий поселок','рп','курортный поселок','кп','село','с','поселок','пос','п','деревня','д','остров','о')


def locality_name(value):
    text=token(value)
    prefixes='|'.join(re.escape(k) for k in sorted(LOCALITY_TYPES,key=len,reverse=True))
    return re.sub(rf'^(?:{prefixes})(?:\.|\s+)\s*','',text)


def street_identity(value):
    text=token(value);types='|'.join(re.escape(k) for k in sorted(STREET_TYPES,key=len,reverse=True))
    prefix=re.match(rf'^({types})(?:\.|\s+)\s*',text);suffix=None if prefix else re.search(rf'\s+({types})\.?$',text)
    kind=None
    if prefix:kind=STREET_TYPES[prefix.group(1)];text=text[prefix.end():]
    elif suffix:kind=STREET_TYPES[suffix.group(1)];text=text[:suffix.start()]
    return kind,re.sub(r'[\s.,]+','',text)


def house_number(value):
    text=token(value);text=re.sub(r'^(?:д\.|дом)\s*','',text)
    text=re.sub(r'\b(?:корпус|корп\.)\s*','к',text)
    text=re.sub(r'\b(?:строение|стр\.)\s*','стр',text)
    text=re.sub(r'\bлитера\s*','',text)
    return text.replace(' ','').replace(',','').translate(str.maketrans({'a':'а','b':'в','c':'с','e':'е','h':'н','k':'к','m':'м','o':'о','p':'р','t':'т','x':'х'}))


def parse_official_address(value):
    match=re.search(r',\s*(?:д\.|дом)\s*(.+)$',value,re.I)
    if not match:
        special=re.fullmatch(r'\s*(.+),\s*ДОС\s+(\d+[\w/ -]*)\s*',value,re.I)
        if not special:return None
        return {'locality':locality_name(special.group(1)),'street_type':'quarter','street_name':'дос','house_number':house_number(special.group(2))}
    prefix=value[:match.start()].split(',')
    if len(prefix)<2:return None
    # Preserve the innermost named locality; the parent city cannot replace it.
    # Microdistrict descriptions remain literal, requiring explicit review.
    kind,name=street_identity(prefix[-1]);town=locality_name(prefix[-2]);house=house_number(match.group(1))
    return {'locality':town,'street_type':kind,'street_name':name,'house_number':house} if town and name and house else None


def address_identity(tags):
    kind,name=street_identity(tags.get('addr:street',''))
    names=sorted({locality_name(tags[k]) for k in ['addr:city','addr:place','addr:village'] if tags.get(k)})
    return {'localities':names,'street_type':kind,'street_name':name,'house_number':house_number(tags.get('addr:housenumber',''))}


def compatible_address(a,b):
    if any(a[k] and b[k] and a[k]!=b[k] for k in ['street_name','house_number']):return False
    if a['street_type'] and b['street_type'] and a['street_type']!=b['street_type']:return False
    return not (a['localities'] and b['localities'] and set(a['localities']).isdisjoint(b['localities']))


def match_allowed(identity,official,locality_context=None):
    """Returns candidate class, never a confirmed geometry match."""
    if identity['street_name']!=official['street_name'] or identity['house_number']!=official['house_number']:return None
    if identity['street_type'] and official['street_type'] and identity['street_type']!=official['street_type']:return None
    if identity['localities']:
        if len(identity['localities'])!=1 or official['locality'] not in identity['localities']:return None
        return 'explicit_address_candidate'
    if locality_context:
        if len(locality_context)!=1 or official['locality'] not in locality_context:return None
        return 'osm_boundary_context_candidate'
    return 'municipal_registry_candidate'


def preferred_geometry_member(features):
    """One deterministic allocation slot for identical geometries, no use inference."""
    return min(features,key=lambda f:(-sum(k in f['properties']['tags'] for k in ['addr:street','addr:housenumber','addr:city','building:levels']),
        f['properties']['osm_type']!='way',f['properties']['osm_id']))


def derived_field(observations):
    if not observations:return {'value':None,'status':'unknown','observations':[]}
    values=[o['value'] for o in observations]
    consistent=all(v==values[0] for v in values)
    return {'value':values[0] if consistent else None,'status':'reported' if consistent else 'conflicting','observations':observations}


def municipality_identity(value):
    text=token(value)
    category='zato' if re.search(r'\bзато\b',text) else 'city' if re.search(r'\b(?:го|городской округ)\b',text) else 'district' if re.search(r'\b(?:мр|муниципальный район)\b',text) else 'municipal'
    name=re.sub(r'\b(?:городской округ|муниципальный округ|муниципальный район|зато|го|мо|мр|город|г|район|округ)\b\.?','',text)
    return re.sub(r'\s+','',name),category
