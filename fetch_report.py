import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta

PLAN_DAILY_BUDGET = 250
PLAN_MONTHLY_LEADS = 400
DAYS_IN_MONTH = 30
PLAN_MONTHLY_BUDGET = PLAN_DAILY_BUDGET * DAYS_IN_MONTH
TARGET_CPL = round(PLAN_MONTHLY_BUDGET / PLAN_MONTHLY_LEADS, 2)

FETCH_SINCE = '2026-03-09'
CHUNK_DAYS = 30
API_VERSION = 'v25.0'
LEAD_ACTION_TYPES = {'lead'}

ACCESS_TOKEN = os.getenv('FACEBOOK_ACCESS_TOKEN')
ACCOUNT_ID = os.getenv('FACEBOOK_ACT_ID')

if not ACCESS_TOKEN or not ACCOUNT_ID:
    print("Ошибка: не заданы FACEBOOK_ACCESS_TOKEN или FACEBOOK_ACT_ID")
    sys.exit(1)

if not ACCOUNT_ID.startswith('act_'):
    ACCOUNT_ID = f'act_{ACCOUNT_ID}'

end_date = datetime.now().strftime('%Y-%m-%d')
start_date = FETCH_SINCE


def date_chunks(since_str, until_str, chunk_days):
    since = datetime.strptime(since_str, '%Y-%m-%d')
    until = datetime.strptime(until_str, '%Y-%m-%d')
    cur = since
    while cur <= until:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), until)
        yield cur.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')
        cur = chunk_end + timedelta(days=1)


def api_get(path, params):
    url = f"https://graph.facebook.com/{API_VERSION}/{path}"
    params = {**params, 'access_token': ACCESS_TOKEN}
    all_data = []

    while url:
        resp = None
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=120)
                if resp.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    print(f"Ошибка запроса к {path}: {e}")
                    sys.exit(1)
                time.sleep(2 ** attempt)
        else:
            print(f"Meta API стабильно возвращает 5xx на {path}")
            sys.exit(1)

        payload = resp.json()
        if 'error' in payload:
            print(f"Meta API вернул ошибку на {path}: {payload['error']}")
            sys.exit(1)

        all_data.extend(payload.get('data', []))
        url = payload.get('paging', {}).get('next')
        params = {}

    return all_data


def api_get_chunked(path, base_params, since, until):
    all_data = []
    for chunk_since, chunk_until in date_chunks(since, until, CHUNK_DAYS):
        params = {**base_params, 'time_range': json.dumps({'since': chunk_since, 'until': chunk_until})}
        all_data.extend(api_get(path, params))
    return all_data


def count_leads(actions):
    total = 0
    for action in actions or []:
        if action.get('action_type') in LEAD_ACTION_TYPES:
            total += int(action.get('value', 0))
    return total


def parse_language(name):
    for p in (name or '').split('_'):
        if p.upper() in ('EN', 'RU'):
            return p.upper()
    return 'RU'


def day_metrics(raw):
    spend = float(raw.get('spend', 0))
    leads = count_leads(raw.get('actions'))
    clicks = int(raw.get('clicks', 0))
    impressions = int(raw.get('impressions', 0))
    return spend, leads, clicks, impressions


def dedup_by_date(raw_rows):
    by_date = {}
    for r in raw_rows:
        by_date[r['date_start']] = r
    result = []
    for date, r in sorted(by_date.items()):
        spend, leads, clicks, impressions = day_metrics(r)
        result.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "impressions": impressions})
    return result


def dedup_by_entity_date(raw_rows, id_field, name_field):
    by_id = {}
    for r in raw_rows:
        entity_id = r.get(id_field)
        entry = by_id.setdefault(entity_id, {
            "id": entity_id,
            "name": r.get(name_field, ''),
            "daily_by_date": {},
        })
        entry["daily_by_date"][r['date_start']] = r

    entities = []
    for entity_id, entry in by_id.items():
        daily = []
        for date, r in sorted(entry["daily_by_date"].items()):
            spend, leads, clicks, impressions = day_metrics(r)
            daily.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "impressions": impressions})
        entities.append({"id": entity_id, "name": entry["name"], "daily": daily})
    return entities


account_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'fields': 'spend,clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)

account_daily = dedup_by_date(account_raw)

campaigns_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'campaign',
    'fields': 'campaign_id,campaign_name,spend,clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
campaigns = dedup_by_entity_date(campaigns_raw, 'campaign_id', 'campaign_name')
for c in campaigns:
    c["language"] = parse_language(c["name"])

adsets_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'adset',
    'fields': 'adset_id,adset_name,spend,clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
adsets = dedup_by_entity_date(adsets_raw, 'adset_id', 'adset_name')

ads_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'ad',
    'fields': 'ad_id,ad_name,spend,clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
creatives = dedup_by_entity_date(ads_raw, 'ad_id', 'ad_name')

ads_meta_raw = api_get(f"{ACCOUNT_ID}/ads", {
    'fields': 'id,creative{thumbnail_url}',
    'limit': 500,
})
thumb_by_ad_id = {a['id']: a.get('creative', {}).get('thumbnail_url') for a in ads_meta_raw}
for c in creatives:
    c["thumbnail_url"] = thumb_by_ad_id.get(c["id"])

age_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'breakdowns': 'age,gender',
    'fields': 'spend,clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)

demo_by_bucket = {}
for r in age_raw:
    bucket = (r.get('age', 'unknown'), r.get('gender', 'unknown'))
    entry = demo_by_bucket.setdefault(bucket, {})
    entry[r['date_start']] = r

age_groups = []
for (age, gender), by_date in demo_by_bucket.items():
    daily = []
    for date, r in sorted(by_date.items()):
        spend, leads, clicks, impressions = day_metrics(r)
        daily.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "impressions": impressions})
    age_groups.append({"age": age, "gender": gender, "daily": daily})

def fetch_reach(since, until):
    raw = api_get(f"{ACCOUNT_ID}/insights", {
        'time_range': json.dumps({'since': since, 'until': until}),
        'fields': 'reach',
        'limit': 1,
    })
    return int(raw[0].get('reach', 0)) if raw else 0


month_start = end_date[:8] + '01'
reach_by_preset = {
    '7d': fetch_reach((datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=6)).strftime('%Y-%m-%d'), end_date),
    '14d': fetch_reach((datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=13)).strftime('%Y-%m-%d'), end_date),
    '30d': fetch_reach((datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=29)).strftime('%Y-%m-%d'), end_date),
    'month': fetch_reach(month_start, end_date),
    'all': fetch_reach(start_date, end_date),
}

report_data = {
    "last_updated": datetime.now().strftime('%d.%m.%Y, %H:%M'),
    "fetched_range": {"since": start_date, "until": end_date},
    "plan": {
        "monthly_budget": PLAN_MONTHLY_BUDGET,
        "monthly_leads": PLAN_MONTHLY_LEADS,
        "target_cpl": TARGET_CPL,
    },
    "account_daily": account_daily,
    "campaigns": campaigns,
    "adsets": adsets,
    "creatives": creatives,
    "age_groups": age_groups,
    "reach_by_preset": reach_by_preset,
}

os.makedirs('data', exist_ok=True)
with open('data/report.json', 'w', encoding='utf-8') as f:
    json.dump(report_data, f, ensure_ascii=False, indent=2)

total_spend = sum(d['spend'] for d in account_daily)
total_leads = sum(d['leads'] for d in account_daily)
total_impressions = sum(d['impressions'] for d in account_daily)

print(f"Готово: {len(account_daily)} дней, {len(campaigns)} кампаний, {len(adsets)} аудиторий, {len(creatives)} объявлений, {len(age_groups)} демо-групп.")
print(f"Итого за период {start_date} — {end_date}: расход ${total_spend:.2f}, лиды {total_leads}, показы {total_impressions}.")
print(f"Охват по периодам: {reach_by_preset}")
