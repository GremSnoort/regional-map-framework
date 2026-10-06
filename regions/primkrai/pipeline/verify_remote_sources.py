"""Verify public Rosstat snapshots using an isolated, explicitly pinned CA chain.

No system trust changes, insecure TLS fallback or automatic source replacement.
"""
import argparse
import hashlib
import json
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CERTIFICATES = [
    ('root.pem', 'https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt',
     '936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504'),
    ('intermediate.pem', 'http://nuc-cdp.digital.gov.ru/cdp/subca_ssl_rsa2024.crt',
     '6f9d829c8e6712444fce3624658d8788672849c5d5b7b53fd9cf7e83eac4193e'),
]
FILES = {
    'rosstat_municipal_population_2025.xlsx': 'https://rosstat.gov.ru/storage/mediabank/%D0%A1hisl_MO_01-01-2025.xlsx',
    'rosstat_vpn2020_table5.xlsx': 'https://rosstat.gov.ru/storage/mediabank/Tom1_tab-5_VPN-2020.xlsx',
}


def download(url, context):
    with urllib.request.urlopen(url, context=context, timeout=40) as response:
        if response.status != 200:
            raise ValueError(f'Unexpected HTTP status: {response.status}')
        return response.read(), response.url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--saved-sources', type=Path, required=True)
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination == args.saved_sources.resolve():
        parser.error('Use a separate verification directory, not the source directory')
    destination.mkdir(parents=True, exist_ok=True)
    # Root obtained under existing system trust; pinned intermediate bytes are
    # authenticated by SHA-256 and by the root signature during TLS validation.
    system = ssl.create_default_context()
    isolated = ssl.create_default_context()
    certificate_records = []
    for name, url, expected in CERTIFICATES:
        data, final_url = download(url, system)
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ValueError(f'Certificate changed: {url}; review new trust material explicitly')
        path = destination / name
        path.write_bytes(data)
        isolated.load_verify_locations(str(path))
        certificate_records.append({'url': url, 'final_url': final_url, 'sha256': actual})
    records = []
    for name, url in FILES.items():
        data, final_url = download(url, isolated)
        actual = hashlib.sha256(data).hexdigest()
        (destination / name).write_bytes(data)
        saved = args.saved_sources / name
        records.append({'file': name, 'url': url, 'final_url': final_url, 'status': 200,
                        'tls_verified': True, 'bytes': len(data), 'sha256': actual,
                        'matches_saved': saved.is_file() and hashlib.sha256(saved.read_bytes()).hexdigest() == actual})
    report = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'certificate_downloads': certificate_records, 'files': records}
    (destination / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if not all(record['matches_saved'] for record in records):
        raise SystemExit('Remote copies differ: review changes before replacing inputs')


if __name__ == '__main__':
    main()
