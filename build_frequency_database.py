"""Build the offline AR8600 broadcast database from HFCC A26 and EiBi A26.

Run with Python 3 (standard library only)::

    python build_frequency_database.py
    python build_frequency_database.py --zip downloaded_hfcc.zip
    python build_frequency_database.py --zip hfcc.zip --eibi-csv sked-a26.csv --eibi-readme README.TXT

ZIP members are identified by their headers, not by internal filenames. The
fixed-width schedule follows the ITU HFBC submission format (columns are
one-based in that specification):
https://www.itu.int/en/ITU-R/terrestrial/broadcast/HFBC/Documents/File%20format%20for%20submission%20of%20HFBC%20requirements-E.pdf

Days are converted from HFCC Sunday=1 to the database's ISO Monday=1. Country
names come from the administration table; transmitter administration is not
used to guess the broadcaster's country. Unknown references retain their code
where useful and produce diagnostics. Notes are copied without reinterpretation.
All HFCC operational records, including DRM, are retained; their existing AM/5
kHz receiver defaults are preserved. EiBi supplies complementary broadcast
schedules and descriptive metadata. Unsupported/irregular schedule qualifiers
are retained in Notes, not silently called daily. EiBi-only engineering fields
remain blank. Merging requires a unique match in both directions, identical
frequency/time/days, compatible dates, and station or site/language evidence.
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
EIBI_CSV_URL = 'http://eibispace.de/dx/sked-a26.csv'
EIBI_README_URL = 'http://www.eibispace.de/dx/README.TXT'
DEFAULT_OUTPUT = Path(__file__).resolve().with_name('AR8600_FREQUENCY_DATABASE_A26.csv')
# Exactly the schema in frequency_database.py / FREQUENCY_DATABASE_CSV.md.
CSV_FIELDS = ('Frequency', 'Station', 'UTCStart', 'UTCEnd', 'Days', 'Band', 'Language',
              'Target', 'TxSite', 'TxCountry', 'StationCountry', 'PowerKW', 'TxAzimuth',
              'TxLatitude', 'TxLongitude', 'Mode', 'Step', 'ValidFrom', 'ValidTo', 'Notes', 'Source')

# Inclusive broadcast-band limits in kHz. Out-of-band schedules are retained.
BROADCAST_BANDS = ((148.5, 283.5, 'LW broadcast'), (520, 1710, 'MW broadcast'),
                   (2300, 2495, '120 m'), (3200, 3400, '90 m'),
                   (3900, 4000, '75 m'), (4750, 5060, '60 m'),
                   (5900, 6200, '49 m'), (7200, 7450, '41 m'),
                   (9400, 9900, '31 m'), (11600, 12100, '25 m'),
                   (13570, 13870, '22 m'), (15100, 15800, '19 m'),
                   (17480, 17900, '16 m'), (18900, 19020, '15 m'),
                   (21450, 21850, '13 m'), (25670, 26100, '11 m'))


class Diagnostics:
    def __init__(self):
        self.input_rows = 0
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
        diagnostics.input_rows += 1
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


class EiBiDiagnostics(Diagnostics):
    def __init__(self):
        super().__init__()
        self.active_broadcast = 0
        self.utility = 0
        self.utility_p = 0
        self.inactive = 0
        self.inactive_p8 = 0
        self.winter_only = 0
        self.outside_scope = 0
        self.unmapped_days = Counter()


def read_eibi_references(text):
    """Read the actual README sections, excluding their overview headings."""
    headings = {}
    for key, roman, title in (('languages', 'I', 'Language codes'),
                              ('countries', 'II', 'Country codes'),
                              ('targets', 'III', 'Target-area codes'),
                              ('sites', 'IV', 'Transmitter[- ]site codes')):
        matches = list(re.finditer(r'^\s+' + roman + r'\)\s+' + title + r'\.\s*$', text, re.MULTILINE))
        if not matches:
            raise ValueError('EiBi README lacks the %s table' % key)
        headings[key] = matches[-1]
    sections = {}
    order = ('languages', 'countries', 'targets', 'sites')
    for index, key in enumerate(order):
        stop = headings[order[index + 1]].start() if index + 1 < len(order) else len(text)
        sections[key] = text[headings[key].end():stop]
    tables = {'languages': {}, 'language_iso': {}, 'countries': {}, 'targets': {}, 'sites': {}}
    for key in ('languages', 'countries'):
        for line in sections[key].splitlines():
            match = re.fullmatch(r' {3}([\w-]+)\s{2,}(.+)', line)
            if match is None:
                continue
            code, name = match.groups()
            if key == 'languages':
                iso = re.search(r'\[([a-z]{3}(?:,[a-z]{3})*)\]', name)
                tables['language_iso'][code] = frozenset(iso[1].split(',')) if iso else frozenset()
                name = name.split(':', 1)[0]
                name = re.sub(r'\s*\[[^]]*\].*$', '', name)
                name = re.sub(r'\s*\([^)]*\)', '', name).strip()
            else:
                name = name.rstrip(' *')
            tables[key][code] = name
    for line in sections['targets'].splitlines():
        match = re.fullmatch(r' {3}([A-Za-z]+)\s+-\s+(.+)', line)
        if match:
            tables['targets'][match[1]] = match[2].split(' (', 1)[0]
    country = None
    for line in sections['sites'].splitlines():
        match = re.match(r'^ {3}([A-Z0-9]{1,3}):\s*(.*)', line)
        if match:
            country, value = match.groups()
        elif country and line.startswith('        ') and not line.startswith('         '):
            value = line.strip()
        else:
            continue
        site = re.match(r'([A-Za-z0-9]+)-(.+)', value)
        if site:
            name = re.split(r'\s+\d{1,2}[NS]\d{2}', site[2], maxsplit=1)[0].strip()
            tables['sites'][(country, site[1])] = name
    if not tables['countries'] or not tables['languages'] or not tables['sites']:
        raise ValueError('Could not reliably read EiBi code tables')
    return tables


EIBI_WEEKDAYS = {'Mo': 1, 'Tu': 2, 'We': 3, 'Th': 4, 'Fr': 5, 'Sa': 6, 'Su': 7}
PERSISTENCE = {0: 'this season only', 1: 'everlasting', 2: 'everlasting; northern DST shift',
               3: 'everlasting; southern DST shift', 4: 'everlasting; winter only',
               5: 'everlasting; summer only', 6: 'partial season'}
# Some currently published entries omit the documented P>=90 utility flag.
# Only explicit service descriptions/codes are used; call signs are not guessed.
UTILITY_LANGUAGE_CODES = {'-CW', '-EC', '-HF', '-TS', '-TY'}
UTILITY_DESCRIPTION = re.compile(
    r'\b(?:navy|naval|volmet|fax|coastguard|coastgd|coast guard|RTTY|FSK|HFDL|STANAG|NAVTEX|DSC|'
    r'teleswitch|military|air force|aero|maritime|GMDSS|COTHEN|meteo|weather|wx|spy numbers)\b|sub comms', re.IGNORECASE)


def eibi_days(value, diagnostics):
    if not value or value.casefold() in ('daily', 'usb', 'lsb'):
        return '1234567'
    if re.fullmatch(r'[1-7]+', value):
        return ''.join(sorted(set(value)))
    if re.fullmatch(r'(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:[-,]?(?:Mo|Tu|We|Th|Fr|Sa|Su))*', value):
        days = set()
        token = r'Mo|Tu|We|Th|Fr|Sa|Su'
        for match in re.finditer(r'(' + token + r')(?:-(' + token + r'))?', value):
            day = EIBI_WEEKDAYS[match[1]]
            last = EIBI_WEEKDAYS[match[2] or match[1]]
            while True:
                days.add(day)
                if day == last:
                    break
                day = day % 7 + 1
        return ''.join(map(str, sorted(days)))
    # Monthly/irregular/comment qualifiers cannot honestly be represented as
    # a daily/weekly schedule. Keep the record and its verbatim qualifier in
    # Notes, and suppress a false automatic on-air indication.
    diagnostics.unmapped_days[value] += 1
    return 'Unknown'


def eibi_dates(persistence, start, stop):
    if persistence != 6:
        return '', ''  # These fields are informational for other P values.
    match = re.fullmatch(r'(\d{4})(?:\[\d{4}\])?', stop)
    if not re.fullmatch(r'\d{4}', start) or match is None:
        raise ValueError('P=6 requires documented DDMM start/stop dates')
    first = date(2026, int(start[2:]), int(start[:2]))
    end = match[1]
    last = date(2026, int(end[2:]), int(end[:2]))
    if last < first:
        last = last.replace(year=2027)
    return first.isoformat(), last.isoformat()


def eibi_country(code, tables, diagnostics):
    name = diagnostics.lookup(tables['countries'], code, 'country')
    return '' if code in ('CLA', 'XUU', 'IW', 'UN') else name


def eibi_site(value, home_country, tables, diagnostics):
    if value.startswith('/'):
        match = re.fullmatch(r'/([A-Z0-9]{1,3})(?:-([A-Za-z0-9]+))?', value)
        if match is None:
            diagnostics.missing[('site syntax', value)] += 1
            return '', '', ''
        country, code = match[1], match[2] or ''
    else:
        country, code = home_country, value
    country_name = eibi_country(country, tables, diagnostics)
    # The README permits blank codes for unknown sites as well as major/sole
    # sites. Home transmitter country is documented, but no specific site is
    # inferred from that absence.
    if not code:
        return '', country_name, country
    name = diagnostics.lookup(tables['sites'], (country, code), 'site')
    if name and re.search(r'unknown|secret|varying', name, re.IGNORECASE):
        diagnostics.missing[('site', (country, code))] += 1
        name = ''
    return name, country_name, country


def eibi_languages(value, tables, diagnostics):
    names, identities = [], set()
    for code in filter(None, value.split(',')):
        names.append(diagnostics.lookup(tables['languages'], code, 'language', code))
        identities.update(tables['language_iso'].get(code, ()))
    return '; '.join(names), frozenset(identities)


def eibi_target(value, tables, diagnostics):
    if not value:
        return ''
    if ',' in value:
        return '; '.join(eibi_target(code, tables, diagnostics) for code in value.split(','))
    if value in tables['countries']:
        return tables['countries'][value]
    if value in tables['targets']:
        return tables['targets'][value]
    for prefix, direction in (('C', 'Central'), ('E', 'East'), ('N', 'North'), ('S', 'South'), ('W', 'West')):
        if value.startswith(prefix) and value[1:] in tables['targets']:
            return direction + ' ' + tables['targets'][value[1:]]
    diagnostics.missing[('target', value)] += 1
    return value


def parse_eibi_schedule(text, tables, diagnostics):
    rows = []
    lines = iter(enumerate(text.splitlines(), 1))
    _, first_line = next(lines, (1, ''))
    reader = csv.reader([first_line], delimiter=';', strict=True)
    header = next(reader, [])
    # The real header has one additional empty trailing field; records do not.
    if header and header[-1] == '':
        header.pop()
    if len(header) != 11 or not header[0].startswith('kHz') or not header[1].startswith('Time(UTC)'):
        raise ValueError('Unexpected EiBi CSV header/field count')
    for line_number, line in lines:
        if not line.strip():
            continue
        diagnostics.input_rows += 1
        try:
            fields = next(csv.reader([line], delimiter=';', strict=True))
            if len(fields) != 11:
                raise ValueError('Expected exactly 11 semicolon-delimited fields')
            frequency, time, day_code, country, station, language, target, site, persistence, start, stop = (v.strip() for v in fields)
            p = int(persistence)
            if p < 0:
                raise ValueError('Negative persistence code')
            if p >= 90:
                diagnostics.utility_p += 1
                diagnostics.utility += 1
                continue
            if p == 8:
                diagnostics.inactive += 1
                diagnostics.inactive_p8 += 1
                continue
            if p == 4:
                # README explicitly says winter-only; A26 is a summer season.
                diagnostics.inactive += 1
                diagnostics.winter_only += 1
                continue
            if p not in PERSISTENCE:
                raise ValueError('Unknown persistence code %r' % persistence)
            khz = number(frequency, 'EiBi frequency (kHz)', minimum=10, maximum=30000)
            if language in UTILITY_LANGUAGE_CODES or UTILITY_DESCRIPTION.search(station):
                diagnostics.utility += 1
                continue
            if not (Decimal('148.5') <= khz <= Decimal('283.5') or 520 <= khz <= 1710 or 2000 <= khz <= 30000):
                diagnostics.outside_scope += 1
                continue
            if not station:
                raise ValueError('Missing station name')
            match = re.fullmatch(r'(\d{4})-(\d{4})', time)
            if match is None:
                raise ValueError('Invalid UTC time range')
            valid_from, valid_to = eibi_dates(p, start, stop)
            specific_day = re.fullmatch(r'(\d{1,2})(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', day_code)
            if p == 6 and specific_day is not None:
                month = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec').index(specific_day[2]) + 1
                day = date(date.fromisoformat(valid_from).year, month, int(specific_day[1]))
                if day.isoformat() < valid_from:
                    day = day.replace(year=day.year + 1)
                if not valid_from <= day.isoformat() <= valid_to:
                    raise ValueError('Days date conflicts with partial-season start/stop dates')
                valid_from = valid_to = day.isoformat()
                days = str(day.isoweekday())
            else:
                days = eibi_days(day_code, diagnostics)
            tx_site, tx_country, tx_code = eibi_site(site, country, tables, diagnostics)
            resolved_language, language_ids = eibi_languages(language, tables, diagnostics)
            mode = next((mode for mode in ('USB', 'LSB', 'CW') if re.search(r'\b' + mode + r'\b', station + ' ' + day_code + ' ' + language)), 'AM')
            if re.search(r'\bDRM\b', station + ' ' + language, re.IGNORECASE):
                mode = ''  # Unsupported digital mode is described, never called AM.
            step = '5 kHz' if khz >= 2000 else '9 kHz'
            if khz < 2000 and tx_code in ('USA', 'CAN', 'MEX', 'CUB', 'B', 'ARG', 'CHL', 'CLM', 'PRU', 'VEN', 'URG'):
                step = '10 kHz'
            if mode != 'AM':
                step = ''  # EiBi gives no receiver step for these unusual modes.
            row = {field: '' for field in CSV_FIELDS}
            row.update(Frequency=format(khz / 1000, '.6f'), Station=station,
                       UTCStart=utc_time(match[1]), UTCEnd=utc_time(match[2], end=True),
                       Days=days, Band=broadcast_band(khz),
                       Language=resolved_language, Target=eibi_target(target, tables, diagnostics),
                       TxSite=tx_site, TxCountry=tx_country,
                       StationCountry=eibi_country(country, tables, diagnostics), Mode=mode, Step=step,
                       ValidFrom=valid_from, ValidTo=valid_to, Source='EiBi A26')
            notes = ['EiBi P=%d (%s)' % (p, PERSISTENCE[p]), 'EiBi ITU=' + country]
            for label, value in (('Days', day_code), ('Language code', language), ('Target code', target),
                                 ('Site code', site), ('Start', start), ('Stop', stop)):
                if value:
                    notes.append('EiBi %s=%s' % (label, value))
            if mode != 'AM':
                notes.append('EiBi unusual mode: ' + (mode or 'DRM'))
            row['Notes'] = '; '.join(notes)
            row['_language_ids'] = language_ids
            row['_persistence'] = p
            row['_tx_country_code'] = tx_code
            rows.append(row)
            diagnostics.active_broadcast += 1
        except (ValueError, ArithmeticError, csv.Error) as error:
            diagnostics.malformed('EiBi CSV', line_number, str(error))
    return rows


def station_identity(value):
    """Conservative spelling/abbreviation normalization, without fuzzy matching."""
    value = value.casefold()
    value = re.sub(r'\bint(?:l)?\.?\b', 'international', value)
    value = re.sub(r'\br\.', 'radio ', value)
    key = re.sub(r'[^a-z0-9]', '', value)
    return 'bbc' if key in ('bbc', 'bbcworldservice') else key


def site_identity(value):
    # Ignore reference annotations, not geographic differences or city names.
    value = re.sub(r'\([^)]*\)|"[0-9]+"|\b[0-9]+\s*kW\b', '', value, flags=re.IGNORECASE)
    return station_identity(value)


def merge_sources(hfcc_rows, eibi_rows, hfcc_tables):
    index = {}
    for i, row in enumerate(hfcc_rows):
        key = (Decimal(row['Frequency']), row['UTCStart'], row['UTCEnd'])
        index.setdefault(key, []).append(i)
    language_codes = {name.casefold(): code.lower() for code, name in hfcc_tables['languages'].items()}
    country_codes = {name.casefold(): code for code, name in hfcc_tables.get('administrations', {}).items()}
    proposals, ambiguous, additions = {}, 0, []
    for row in eibi_rows:
        key = (Decimal(row['Frequency']), row['UTCStart'], row['UTCEnd'])
        candidates = index.get(key, [])
        matches = []
        for i in candidates:
            hfcc = hfcc_rows[i]
            if row['Days'] == 'Unknown' or row['Days'] != hfcc['Days'] or row['_persistence'] == 4:
                continue
            if row['ValidFrom'] or row['ValidTo']:
                if (row['ValidFrom'], row['ValidTo']) != (hfcc['ValidFrom'], hfcc['ValidTo']):
                    continue
            elif not (hfcc['ValidFrom'] <= '2026-03-29' and hfcc['ValidTo'] >= '2026-10-24'):
                # An unspecified/full-season EiBi schedule cannot replace a
                # narrower HFCC date range without losing information.
                continue
            hfcc_languages = frozenset(language_codes.get(name.casefold(), name.casefold())
                                       for name in hfcc['Language'].split('; ') if name)
            language_agrees = bool(row['_language_ids']) and row['_language_ids'] == hfcc_languages
            if row['_language_ids'] and hfcc_languages and not language_agrees:
                continue
            station_agrees = station_identity(row['Station']) == station_identity(hfcc['Station'])
            sites_known = bool(row['TxSite'] and hfcc['TxSite'])
            site_agrees = sites_known and site_identity(row['TxSite']) == site_identity(hfcc['TxSite'])
            if sites_known and not site_agrees:
                continue
            hfcc_country = country_codes.get(hfcc['TxCountry'].casefold())
            if hfcc_country and row['TxCountry'] and row.get('_tx_country_code') and hfcc_country != row['_tx_country_code']:
                continue
            if row['Mode'] != 'AM' or re.search(r'\bDRM\b', hfcc['Notes'], re.IGNORECASE):
                continue
            if station_agrees or (len(candidates) == 1 and site_agrees and language_agrees):
                matches.append(i)
        # A second candidate at identical frequency/time is deliberately not
        # resolved by heuristically picking the closest station/site.
        if len(candidates) == 1 and len(matches) == 1:
            proposals.setdefault(matches[0], []).append(row)
        else:
            if candidates:
                ambiguous += 1
            additions.append(row)
    result = [dict(row) for row in hfcc_rows]
    merged = 0
    for i, proposed in proposals.items():
        if len(proposed) != 1:
            ambiguous += len(proposed)
            additions.extend(proposed)
            continue
        incoming = proposed[0]
        for field in ('Station', 'StationCountry', 'Language', 'Target', 'TxSite', 'TxCountry'):
            if not result[i][field] and incoming[field]:
                result[i][field] = incoming[field]
        result[i]['Notes'] = '; '.join(filter(None, (result[i]['Notes'], incoming['Notes'])))
        result[i]['Source'] = 'HFCC A26; EiBi A26'
        merged += 1
    result.extend({field: row[field] for field in CSV_FIELDS} for row in additions)
    result.sort(key=lambda row: (Decimal(row['Frequency']), row['UTCStart'], row['Station']))
    return result, merged, len(additions), ambiguous


def download(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'AR8600-broadcast-database/2.0'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def representative_rows(rows, count=4):
    seen = set()
    for row in rows:
        if row['Station'] not in seen:
            seen.add(row['Station'])
            yield row
        if len(seen) >= count:
            break


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--url', default=SOURCE_URL, help='HFCC A26 operational ZIP URL')
    parser.add_argument('--zip', type=Path, help='Use a local HFCC ZIP instead of downloading it')
    parser.add_argument('--eibi-csv', type=Path, help='Use a downloaded EiBi A26 CSV')
    parser.add_argument('--eibi-readme', type=Path, help='Use a downloaded EiBi README/code tables')
    parser.add_argument('--eibi-url', default=EIBI_CSV_URL, help='EiBi A26 semicolon CSV URL')
    parser.add_argument('--eibi-readme-url', default=EIBI_README_URL, help='EiBi README/code tables URL')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT, help='Output UTF-8 CSV path')
    args = parser.parse_args(argv)
    try:
        if args.zip:
            data = args.zip.read_bytes()
        else:
            data = download(args.url)
        eibi_csv = args.eibi_csv.read_bytes() if args.eibi_csv else download(args.eibi_url)
        eibi_readme = args.eibi_readme.read_bytes() if args.eibi_readme else download(args.eibi_readme_url)
        members = inspect_archive(data)
        diagnostics = Diagnostics()
        hfcc_tables = read_references(members, diagnostics)
        hfcc_rows = parse_schedule(members, hfcc_tables, diagnostics)
        if not hfcc_rows:
            raise ValueError('No valid HFCC operational records found')
        eibi_diagnostics = EiBiDiagnostics()
        eibi_tables = read_eibi_references(decode_member(eibi_readme))
        eibi_rows = parse_eibi_schedule(decode_member(eibi_csv), eibi_tables, eibi_diagnostics)
        rows, merged, added, ambiguous = merge_sources(hfcc_rows, eibi_rows, hfcc_tables)
        for kind, (name, _) in sorted(members.items()):
            print('ZIP %s: %s' % (kind, name), file=sys.stderr)
        for source, source_diagnostics in (('HFCC', diagnostics), ('EiBi', eibi_diagnostics)):
            for (kind, code), count in sorted(source_diagnostics.missing.items(), key=lambda item: (item[0][0], str(item[0][1]))):
                print('%s unresolved %s %r: %d occurrence(s); no reference information invented' %
                      (source, kind, code, count), file=sys.stderr)
        for code, count in sorted(eibi_diagnostics.unmapped_days.items()):
            if not count:
                continue
            print('EiBi Days %r: %d record(s); qualifier retained in Notes, automatic on-air status unknown' %
                  (code, count), file=sys.stderr)
        if not eibi_diagnostics.utility_p and eibi_diagnostics.utility:
            print('EiBi discrepancy: no P>=90 flags; explicit utility service names/language codes were excluded.', file=sys.stderr)
        with args.output.open('w', newline='', encoding='utf-8-sig') as destination:
            writer = csv.DictWriter(destination, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print('Output: %s' % args.output.resolve())
        print('HFCC input rows: %d' % diagnostics.input_rows)
        print('EiBi raw input rows: %d' % eibi_diagnostics.input_rows)
        print('EiBi active broadcast input rows: %d' % eibi_diagnostics.active_broadcast)
        print('EiBi utility rows excluded: %d (P>=90: %d; explicit service/codes: %d)' %
              (eibi_diagnostics.utility, eibi_diagnostics.utility_p, eibi_diagnostics.utility - eibi_diagnostics.utility_p))
        print('EiBi inactive rows excluded: %d (P=8: %d; winter-only P=4: %d)' %
              (eibi_diagnostics.inactive, eibi_diagnostics.inactive_p8, eibi_diagnostics.winter_only))
        print('EiBi out-of-broadcast-scope rows excluded: %d' % eibi_diagnostics.outside_scope)
        print('Confirmed HFCC/EiBi merges: %d' % merged)
        print('EiBi-only rows added: %d' % added)
        print('Ambiguous possible duplicates kept separately: %d' % ambiguous)
        print('Final total rows: %d' % len(rows))
        print('Frequency range: %s - %s MHz' % (rows[0]['Frequency'], rows[-1]['Frequency']))
        for band in ('LW broadcast', 'MW broadcast'):
            print('%s row count: %d' % (band, sum(row['Band'] == band for row in rows)))
        print('SW row count: %d' % sum(Decimal(row['Frequency']) >= 2 for row in rows))
        print('Records with StationCountry: %d' % sum(bool(row['StationCountry']) for row in rows))
        print('Records with TxCountry: %d' % sum(bool(row['TxCountry']) for row in rows))
        print('Records with PowerKW: %d' % sum(bool(row['PowerKW']) for row in rows))
        print('Records with coordinates: %d' % sum(bool(row['TxLatitude'] and row['TxLongitude']) for row in rows))
        print('Records with TxAzimuth: %d' % sum(bool(row['TxAzimuth']) for row in rows))
        print('HFCC malformed/skipped schedule/reference records: %d / %d' % (diagnostics.skipped, diagnostics.reference_errors))
        print('EiBi malformed/skipped records: %d' % eibi_diagnostics.skipped)
        for source, source_diagnostics in (('HFCC', diagnostics), ('EiBi', eibi_diagnostics)):
            for kind in ('country', 'language', 'site'):
                missing = {code: count for (label, code), count in source_diagnostics.missing.items()
                           if label == kind or kind == 'country' and label == 'administration'}
                print('%s unresolved %s codes: %d distinct, %d occurrences' %
                      (source, kind, len(missing), sum(missing.values())))
        for label, selected in (
                ('Representative merged records', list(representative_rows(row for row in rows if row['Source'] == 'HFCC A26; EiBi A26'))),
                ('EiBi-only LW records', list(representative_rows(row for row in rows if row['Source'] == 'EiBi A26' and row['Band'] == 'LW broadcast'))),
                ('EiBi-only MW records', list(representative_rows(row for row in rows if row['Source'] == 'EiBi A26' and row['Band'] == 'MW broadcast')))):
            print(label + ':')
            if not selected:
                print('  None in downloaded source; no records invented.')
            for row in selected:
                print('  ' + repr({field: row[field] for field in
                    ('Frequency', 'Station', 'UTCStart', 'UTCEnd', 'Days', 'Language', 'Target',
                     'TxSite', 'TxCountry', 'StationCountry', 'PowerKW', 'Source')}))
    except (OSError, ValueError, csv.Error, zipfile.BadZipFile, urllib.error.URLError) as error:
        print('Broadcast database generation failed: %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
