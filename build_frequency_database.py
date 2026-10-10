"""Build the offline AR8600 database from the HFCC A26 operational ZIP.

Run with Python 3 (standard library only)::

    python build_frequency_database.py
    python build_frequency_database.py --zip downloaded_hfcc.zip

ZIP members are identified by their headers, not by internal filenames. The
fixed-width schedule follows the ITU HFBC submission format (columns are
one-based in that specification):
https://www.itu.int/en/ITU-R/terrestrial/broadcast/HFBC/Documents/File%20format%20for%20submission%20of%20HFBC%20requirements-E.pdf

Days are converted from HFCC Sunday=1 to the database's ISO Monday=1. Country
names come from the administration table; transmitter administration is not
used to guess the broadcaster's country. Unknown references retain their code
where useful and produce diagnostics. Notes are copied without reinterpretation.
All operational records, including DRM, are retained; AM/5 kHz are the requested
receiver defaults, rather than an assertion that every transmission is analogue.
"""

import argparse
import csv
import io
import re
import sys
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path


SOURCE_URL = 'https://new.hfcc.org/data/a26/a26allx2.zip'
DEFAULT_OUTPUT = Path(__file__).resolve().with_name('AR8600_FREQUENCY_DATABASE_A26.csv')
# Exactly the schema in frequency_database.py / FREQUENCY_DATABASE_CSV.md.
CSV_FIELDS = ('Frequency', 'Station', 'UTCStart', 'UTCEnd', 'Days', 'Band', 'Language',
              'Target', 'TxSite', 'TxCountry', 'StationCountry', 'PowerKW', 'TxAzimuth',
              'TxLatitude', 'TxLongitude', 'Mode', 'Step', 'ValidFrom', 'ValidTo', 'Notes', 'Source')

# Inclusive broadcast-band limits in kHz. Out-of-band schedules are retained.
BROADCAST_BANDS = ((2300, 2495, '120 m'), (3200, 3400, '90 m'),
                   (3900, 4000, '75 m'), (4750, 5060, '60 m'),
                   (5900, 6200, '49 m'), (7200, 7450, '41 m'),
                   (9400, 9900, '31 m'), (11600, 12100, '25 m'),
                   (13570, 13870, '22 m'), (15100, 15800, '19 m'),
                   (17480, 17900, '16 m'), (18900, 19020, '15 m'),
                   (21450, 21850, '13 m'), (25670, 26100, '11 m'))


class Diagnostics:
    def __init__(self):
        self.skipped = 0
        self.reference_errors = 0
        self.missing = Counter()

    def malformed(self, member, line_number, reason, reference=False):
        if reference:
            self.reference_errors += 1
        else:
            self.skipped += 1
        print('%s:%d: skipped malformed %s: %s' %
              (member, line_number, 'reference' if reference else 'schedule record', reason),
              file=sys.stderr)

    def lookup(self, table, code, kind, fallback=''):
        if not code:
            return fallback
        if code not in table:
            self.missing[(kind, code)] += 1
            return fallback
        return table[code]


def decode_member(raw):
    # The schedule/older references are Latin-1; the language table is UTF-8.
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('iso-8859-1')


def inspect_archive(data):
    """Discover the operational schedule and required reference tables by content."""
    found = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            text = decode_member(archive.read(member))
            header = '\n'.join(text.splitlines()[:20]).upper()
            kind = None
            if 'GLOBAL HF SCHEDULE' in header and 'CIRAF' in header and 'STRT' in header:
                if not re.search(r'^;\s*A26\s+ALL\b', text, re.MULTILINE):
                    raise ValueError('Operational schedule is not the requested A26 ALL season')
                kind = 'schedule'
            elif 'ADMINISTRATION ENGLISH NAME' in header:
                kind = 'administrations'
            elif 'SITE NAME' in header and 'LATI' in header and 'LONGI' in header:
                kind = 'sites'
            elif re.search(r'^;CO\s+BROADCASTER\s*$', header, re.MULTILINE):
                kind = 'broadcasters'
            elif 'REFERENCE TABLE LANGUAGE' in header:
                kind = 'languages'
            elif 'ANTENNA DEFINITION' in header:
                kind = 'antennas'
            if kind is not None:
                if kind in found:
                    raise ValueError('Ambiguous ZIP: multiple %s members' % kind)
                found[kind] = (member.filename, text)
    required = {'schedule', 'administrations', 'sites', 'broadcasters', 'languages', 'antennas'}
    missing = required - found.keys()
    if missing:
        raise ValueError('ZIP lacks required tables: ' + ', '.join(sorted(missing)))
    return found


def data_lines(text):
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.strip() and not line.lstrip().startswith(';'):
            yield line_number, line


def coordinate(value, latitude):
    # Separate patterns keep latitude/longitude hemispheres strict.
    match = re.fullmatch(r'(\d{1,3})([NS])(\d{2})' if latitude else
                         r'(\d{1,3})([EW])(\d{2})', value)
    if match is None:
        raise ValueError('Invalid coordinate ' + repr(value))
    degrees, hemisphere, minutes = match.groups()
    degrees, minutes = int(degrees), int(minutes)
    limit = 90 if latitude else 180
    if minutes >= 60 or degrees > limit or (degrees == limit and minutes):
        raise ValueError('Out-of-range coordinate ' + repr(value))
    number = Decimal(degrees) + Decimal(minutes) / 60
    if hemisphere in 'SW':
        number = -number
    return format(number, '.6f')


def read_references(members, diagnostics):
    tables = {}
    for kind in ('administrations', 'broadcasters', 'languages', 'antennas', 'sites'):
        member, text = members[kind]
        table = {}
        for line_number, line in data_lines(text):
            try:
                code = line[:3].strip()
                if not code or len(line) < 5 or line[3] != ' ':
                    raise ValueError('Missing code/name separator')
                if kind == 'sites':
                    # Some historical site records have one fewer padding space.
                    name, country, lat, lon = line[4:].rsplit(None, 3)
                    value = {'name': name.strip(), 'country': country,
                             'latitude': coordinate(lat, True),
                             'longitude': coordinate(lon, False)}
                else:
                    value = line[4:54].strip() if kind == 'administrations' else line[4:].strip()
                    if not value:
                        raise ValueError('Missing reference name')
                if code in table and table[code] != value:
                    raise ValueError('Conflicting duplicate reference ' + code)
                table[code] = value
            except ValueError as error:
                diagnostics.malformed(member, line_number, str(error), reference=True)
        tables[kind] = table
    return tables


def broadcast_band(frequency_khz):
    return next((label for low, high, label in BROADCAST_BANDS
                 if low <= frequency_khz <= high), 'Outside broadcast bands')


def utc_time(value, end=False):
    if not re.fullmatch(r'\d{4}', value):
        raise ValueError('Invalid UTC time ' + repr(value))
    hour, minute = int(value[:2]), int(value[2:])
    if (hour, minute) != (24, 0) or not end:
        if hour > 23 or minute > 59:
            raise ValueError('Out-of-range UTC time ' + value)
    return '%02d:%02d' % (hour, minute)


def iso_date(value):
    if not re.fullmatch(r'\d{6}', value):
        raise ValueError('Invalid DDMMYY date ' + repr(value))
    return date(2000 + int(value[4:]), int(value[2:4]), int(value[:2])).isoformat()


def iso_days(value):
    if not re.fullmatch(r'[1-7]+', value) or len(set(value)) != len(value):
        raise ValueError('Invalid HFCC weekdays ' + repr(value))
    return ''.join(str(day) for day in sorted({(int(day) + 5) % 7 + 1 for day in value}))


def number(value, label, minimum=0, maximum=None):
    if not re.fullmatch(r'\d+(?:\.\d+)?', value):
        raise ValueError('Invalid %s %r' % (label, value))
    parsed = Decimal(value)
    if parsed < minimum or maximum is not None and parsed > maximum:
        raise ValueError('Out-of-range ' + label)
    return parsed


def azimuth(value, antenna):
    # ITU-R BS.705: HQ/HX are omnidirectional horizontal arrays; VM is a
    # vertical monopole. Zero alone is ambiguous: directional North is valid.
    if not value or value.upper() in ('ND', 'N/D'):
        return ''
    bearing = number(value, 'azimuth', maximum=360)
    if re.match(r'^(HQ|HX|VM|ND)', antenna) or re.search(r'NON[- ]?DIRECTIONAL|OMNIDIRECTIONAL', antenna.upper()):
        return ''
    return format(bearing, 'f')


def language_names(value, table, diagnostics):
    if not value:
        return ''
    # HFCC concatenates three-character language codes (e.g. EngSpaCmn).
    if not re.fullmatch(r'(?:[A-Za-z]{3})+', value):
        diagnostics.missing[('language field', value)] += 1
        return value
    return '; '.join(diagnostics.lookup(table, code, 'language', code)
                     for code in (value[i:i + 3] for i in range(0, len(value), 3)))


def parse_schedule(members, tables, diagnostics):
    rows = []
    member, text = members['schedule']
    for line_number, line in data_lines(text):
        try:
            if len(line) < 124:
                raise ValueError('Truncated fixed-width schedule record')
            frequency = number(line[:5].strip(), 'frequency (kHz)', minimum=2000, maximum=30000)
            start, end = utc_time(line[6:10]), utc_time(line[11:15], end=True)
            days = iso_days(line[72:79].strip())
            valid_from, valid_to = iso_date(line[80:86]), iso_date(line[87:93])
            if valid_from > valid_to:
                raise ValueError('Reversed validity dates')
            power = line[51:55].strip()
            if power:
                power = format(number(power, 'power (kW)'), 'f')
            if line[94:95] not in ('D', 'N', 'T'):
                raise ValueError('Unknown HFCC modulation ' + repr(line[94:95]))
            site_code, broadcaster = line[47:50].strip(), line[117:120].strip()
            site = diagnostics.lookup(tables['sites'], site_code, 'site')
            administration = site['country'] if site else line[113:116].strip()
            country = diagnostics.lookup(tables['administrations'], administration, 'administration')
            antenna_code = line[68:71].strip()
            antenna = diagnostics.lookup(tables['antennas'], antenna_code, 'antenna')
            row = {field: '' for field in CSV_FIELDS}
            row.update(Frequency=format(frequency / 1000, '.6f'),
                       Station=diagnostics.lookup(tables['broadcasters'], broadcaster, 'broadcaster', broadcaster),
                       UTCStart=start, UTCEnd=end, Days=days, Band=broadcast_band(frequency),
                       Language=language_names(line[102:112].strip(), tables['languages'], diagnostics),
                       Target=line[16:46].strip(), TxSite=site['name'] if site else '',
                       TxCountry=country, PowerKW=power,
                       TxAzimuth=azimuth(line[56:63].strip(), antenna),
                       TxLatitude=site['latitude'] if site else '',
                       TxLongitude=site['longitude'] if site else '',
                       Mode='AM', Step='5 kHz', ValidFrom=valid_from, ValidTo=valid_to,
                       Notes=line[151:].strip(), Source='HFCC A26')
            rows.append(row)
        except ValueError as error:
            diagnostics.malformed(member, line_number, str(error))
    # Stable sorting retains distinct transmissions with identical sort keys.
    rows.sort(key=lambda row: (Decimal(row['Frequency']), row['UTCStart'], row['Station']))
    return rows


def build_database(data):
    members = inspect_archive(data)
    diagnostics = Diagnostics()
    tables = read_references(members, diagnostics)
    rows = parse_schedule(members, tables, diagnostics)
    if not rows:
        raise ValueError('No valid operational schedule records found')
    return rows, diagnostics, members


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--url', default=SOURCE_URL, help='HFCC A26 operational ZIP URL')
    parser.add_argument('--zip', type=Path, help='Use an already downloaded ZIP (no network access)')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT, help='Output UTF-8 CSV path')
    args = parser.parse_args(argv)
    try:
        if args.zip:
            data = args.zip.read_bytes()
        else:
            request = urllib.request.Request(args.url, headers={'User-Agent': 'AR8600-HFCC-database/1.0'})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
        rows, diagnostics, members = build_database(data)
        for kind, (name, _) in sorted(members.items()):
            print('ZIP %s: %s' % (kind, name), file=sys.stderr)
        for (kind, code), count in sorted(diagnostics.missing.items()):
            print('Unresolved %s %r: %d occurrence(s); no reference information invented' %
                  (kind, code, count), file=sys.stderr)
        with args.output.open('w', newline='', encoding='utf-8-sig') as destination:
            writer = csv.DictWriter(destination, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print('Output: %s' % args.output.resolve())
        print('Rows: %d' % len(rows))
        print('Frequency range: %s - %s MHz' % (rows[0]['Frequency'], rows[-1]['Frequency']))
        print('Records with PowerKW: %d' % sum(bool(row['PowerKW']) for row in rows))
        print('Records with coordinates: %d' % sum(bool(row['TxLatitude'] and row['TxLongitude']) for row in rows))
        print('Records with TxAzimuth: %d' % sum(bool(row['TxAzimuth']) for row in rows))
        print('Malformed/skipped schedule records: %d' % diagnostics.skipped)
        print('Malformed/skipped reference records: %d' % diagnostics.reference_errors)
        print('StationCountry left blank: the archive supplies no structured broadcaster-country field.')
    except (OSError, ValueError, zipfile.BadZipFile, urllib.error.URLError) as error:
        print('HFCC database generation failed: %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
