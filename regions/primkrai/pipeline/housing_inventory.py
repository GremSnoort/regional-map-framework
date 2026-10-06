"""Housing evidence v2: purpose, measurement semantics, matching and occupancy.

Official attributes and geometry linkage are separate checks. A published field
is not automatically a verified observation for a particular OSM contour.
"""
from __future__ import annotations
import math
import re
import unicodedata

FIELDS = ('house_type','use','above_ground_storeys','gross_building_area_m2','premises_total_area_m2',
          'residential_premises_area_m2','apartments','occupied_fraction','mean_household_size')
STATUS = {'unknown','reported','conflicting','confirmed'}


def token(value):
    value=unicodedata.normalize('NFKC',str(value)).lower().replace('ё','е')
    return re.sub(r'\s+',' ',value).strip()


def locality(value):
    value=token(value)
    return re.sub(r'^(?:г\.?|город|с\.?|село|пгт\.?|п\.?|пос\.?|поселок|д\.?|деревня)\s+','',value)


def street(value):
    value=token(value)
    value=re.sub(r'\b(?:улица|ул\.?|проспект|пр-кт|пр-т|переулок|пер\.?|квартал|кв-л|микрорайон|мкр\.?|бульвар|бул\.?|шоссе|ш\.)\s*','',value)
    return re.sub(r'[\s.,]+','',value)


def number(value):
    # Preserve slash, corpus and structure: 6/4 != 6 and 6к1 != 6.
    value=token(value).replace(' ','')
    return value.translate(str.maketrans({'a':'а','b':'в','c':'с','e':'е','h':'н','k':'к','m':'м','o':'о','p':'р','t':'т','x':'х'}))


def parse_address(text):
    pieces=[p.strip() for p in str(text).split(',')]
    match=re.search(r'(?:^|,)\s*д\.\s*(.+)$',str(text))
    if len(pieces)<3 or not match:
        return None
    town=locality(pieces[0]);name=street(','.join(pieces[1:-1]));house=number(match.group(1))
    return (town,name,house) if town and name and house else None


def positive(value, integer=False, maximum=None):
    if value in ('',None) or isinstance(value,bool):return None
    try:n=float(str(value).replace(',','.').strip())
    except (TypeError,ValueError):return None
    if not math.isfinite(n) or n<=0 or integer and n!=int(n) or maximum is not None and n>maximum:return None
    return int(n) if integer else n


def unknown():
    return {'value':None,'status':'unknown','observations':[]}


def observation(value,source_id,locator,scope=None):
    return {'value':value,'source_id':source_id,'locator':locator,'as_of':None,'scope':scope}


def reconcile(observations):
    if not observations:return unknown()
    values=[o['value'] for o in observations]
    # Same semantics only. Different area fields are never pooled here.
    return {'value':values[0] if all(v==values[0] for v in values) else None,
            'status':'reported' if all(v==values[0] for v in values) else 'conflicting','observations':observations}


def check_area_consistency(gross,premises,residential):
    flags=[]
    if gross is not None and premises is not None and premises>gross*1.01:flags.append('premises_area_exceeds_gross')
    if premises is not None and residential is not None and residential>premises*1.01:flags.append('residential_area_exceeds_all_premises')
    if gross is not None and residential is not None and residential>gross*1.01:flags.append('residential_area_exceeds_gross')
    return flags


def confirmed_weight(record,footprint_area_m2,sources):
    """Compile only reviewed, uniquely linked, non-conflicting observations.

    Return a method/unit with its weight: apartments and m² cannot be combined
    in one allocation without a separately calibrated conversion.
    """
    match=record['match']
    if match.get('status')!='confirmed':return None
    if not match.get('reviewer') or not match.get('reviewed_at') or not match.get('canonical_house_id'):
        raise ValueError('Geometry match needs reviewed canonical house identity')
    fields=record['fields']
    def get(name):
        field=fields[name]
        if field['status']!='confirmed':return None
        if not field.get('reviewer') or not field.get('reviewed_at') or not field['observations']:
            raise ValueError('Verified measurement needs review and source')
        if any(sources.get(o['source_id'],{}).get('kind')!='primary' or not o.get('as_of') for o in field['observations']):
            raise ValueError('Confirmation requires dated primary observations')
        values=[o['value'] for o in field['observations']]
        if any(v!=field['value'] for v in values):raise ValueError('Conflicting observations cannot be confirmed')
        return field['value']
    use=get('use')
    if use=='non_residential':return {'method':'exclude','unit':'none','weight':0.}
    if use not in {'residential','mixed'}:return None
    area=get('residential_premises_area_m2')
    if area is not None:
        if positive(area) is None or any(o.get('scope')!='whole_contour_residential_premises' for o in fields['residential_premises_area_m2']['observations']):raise ValueError('Residential area needs correct scope')
        if record.get('area_consistency_flags') or any(fields[n]['status']=='conflicting' for n in ['gross_building_area_m2','premises_total_area_m2']):raise ValueError('Unresolved area inconsistency')
        return {'method':'residential_area','unit':'m2','weight':float(area)}
    apartments,occupied,household=[get(n) for n in ['apartments','occupied_fraction','mean_household_size']]
    if apartments is not None and occupied is not None and household is not None:
        if positive(apartments,integer=True) is None or not isinstance(occupied,(int,float)) or isinstance(occupied,bool) or not math.isfinite(occupied) or not 0<=occupied<=1 or positive(household) is None:raise ValueError('Invalid occupancy-based weight')
        dates={o['as_of'] for n in ['apartments','occupied_fraction','mean_household_size'] for o in fields[n]['observations']}
        if None in dates or len(dates)!=1:raise ValueError('Occupancy inputs require a consistent reference date')
        return {'method':'occupied_apartments','unit':'persons_proxy','weight':apartments*occupied*household}
    floors=get('above_ground_storeys')
    if floors is not None and use=='residential':
        if positive(footprint_area_m2) is None:raise ValueError('Footprint area must be positive and finite')
        if positive(floors,integer=True,maximum=80) is None or any(o.get('scope')!='uniform_above_ground_whole_contour' for o in fields['above_ground_storeys']['observations']):raise ValueError('Floor count scope not verified')
        return {'method':'floor_area_proxy','unit':'m2_proxy','weight':footprint_area_m2*floors}
    return None


def allocate_verified_population(population,weights):
    """Normalize comparable verified weights without mixing persons and m²."""
    from build_density import allocate
    units={w['unit'] for w in weights.values()}
    if len(units)!=1 or 'none' in units:raise ValueError('Weights require one calibrated unit')
    values={k:w['weight'] for k,w in weights.items()}
    if not values or any(not math.isfinite(v) or v<0 for v in values.values()) or sum(values.values())<=0:raise ValueError('Nonpositive or invalid allocation weights')
    return allocate(population,values)


def compile_verified_weights(records,sources):
    result={};canonical=set();osm_ids=set()
    for record in records:
        if record['osm_id'] in osm_ids:raise ValueError('Duplicate OSM contour in inventory')
        osm_ids.add(record['osm_id'])
        if record['match'].get('status')=='confirmed':
            identity=record['match'].get('canonical_house_id')
            if identity in canonical:raise ValueError('Same official house cannot supply weight for multiple contours')
            canonical.add(identity)
        weight=confirmed_weight(record,record['footprint_area_m2'],sources)
        if weight is not None:result[record['osm_id']]=weight
    return result
