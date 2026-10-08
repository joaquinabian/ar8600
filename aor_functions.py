__author__ = 'joaquin'

import re
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP


BANDSCOPE_SPANS = {1: 10000000, 2: 5000000, 3: 2000000, 4: 1000000,
                   5: 500000, 6: 200000, 7: 100000}
BANDSCOPE_SPAN_LABELS = ('10 MHz', '5 MHz', '2 MHz', '1 MHz', '500 kHz', '200 kHz', '100 kHz')
BANDSCOPE_MIN_FREQUENCY_HZ = 100000
BANDSCOPE_MAX_FREQUENCY_HZ = 2040000000


def bandscope_resolution(span_code):
    if span_code not in BANDSCOPE_SPANS:
        raise ValueError('Invalid bandscope span')
    return 2000 if span_code in (6, 7) else 10000


def bandscope_centre_command(frequency_mhz):
    if not re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,6})?', frequency_mhz):
        raise ValueError('Enter MHz with up to six decimal places')
    frequency_hz = int(Decimal(frequency_mhz) * 1000000)
    if not BANDSCOPE_MIN_FREQUENCY_HZ <= frequency_hz <= BANDSCOPE_MAX_FREQUENCY_HZ:
        raise ValueError('Centre frequency must be between %g and %g MHz' %
                         (BANDSCOPE_MIN_FREQUENCY_HZ / 1000000, BANDSCOPE_MAX_FREQUENCY_HZ / 1000000))
    return ('CF%010d\r\n' % frequency_hz).encode('ascii')


def bandscope_marker_frequency(status, target_hz):
    """Snap to a CF-relative sample, inside both the span and receiver range."""
    centre = status['centre_hz']
    spacing = bandscope_resolution(status['span_code'])
    low = max(centre - status['span_hz'] // 2, BANDSCOPE_MIN_FREQUENCY_HZ)
    high = min(centre + status['span_hz'] // 2, BANDSCOPE_MAX_FREQUENCY_HZ)
    minimum = int((Decimal(low - centre) / spacing).to_integral_value(rounding=ROUND_CEILING))
    maximum = int((Decimal(high - centre) / spacing).to_integral_value(rounding=ROUND_FLOOR))
    if minimum > maximum:
        raise ValueError('No valid marker frequency in this span')
    target = Decimal(str(target_hz))
    if not target.is_finite():
        raise ValueError('Invalid marker frequency')
    offset = int(((target - centre) / spacing).to_integral_value(rounding=ROUND_HALF_UP))
    return centre + max(minimum, min(maximum, offset)) * spacing


def parse_bandscope_status(text):
    match = re.fullmatch(r'AM PH([01]) CF([0-9]{10}) MF([0-9]{10}) SW([1-7])', text)
    if match is None:
        raise ValueError('Invalid bandscope status')
    peak_hold, centre, marker, span = match.groups()
    return {'peak_hold': bool(int(peak_hold)), 'centre_hz': int(centre),
            'marker_hz': int(marker), 'span_code': int(span),
            'span_hz': BANDSCOPE_SPANS[int(span)]}


def parse_ds_block(text):
    match = re.fullmatch(r'DS([0-9]{4})\s*:\s*([0-9A-Fa-f]{16})[ \t]*([0-9A-Fa-f]{16})', text)
    if match is None:
        raise ValueError('Invalid DS block: expected an index and 32 hex samples')
    index = int(match.group(1))
    if not 31 <= index <= 1023 or index % 32 != 31:
        raise ValueError('Invalid DS block index')
    return index, tuple(int(value, 16) for value in match.group(2) + match.group(3))


class BandscopeSweep:
    """Assemble one DS dump, retaining invalid/incomplete state until its end."""
    def __init__(self, sample_counts=(1024,)):
        self.sample_counts = sample_counts
        self.blocks = {}
        self.failed = False
        self.finished = False

    def add_line(self, text):
        try:
            index, values = parse_ds_block(text)
        except ValueError:
            self.failed = True
            # Even a damaged final block ends this dump; never display it.
            if re.match(r'DS0031\b', text):
                self.finished = True
            raise
        if self.finished:
            raise ValueError('DS data received after the sweep ended')
        if index >= max(self.sample_counts):
            self.failed = True
            raise ValueError('DS block outside the requested sweep')
        if index in self.blocks:
            if self.blocks[index] != values:
                self.failed = True
                raise ValueError('Conflicting duplicate DS block')
            return None  # Identical duplicates neither count nor overwrite.
        self.blocks[index] = values
        if index == 31:
            self.finished = True
            sample_count = len(self.blocks) * 32
            if (sample_count not in self.sample_counts or
                    set(self.blocks) != set(range(31, sample_count, 32))):
                self.failed = True
                raise ValueError('Incomplete DS sweep: %d blocks' % len(self.blocks))
        if not self.finished or self.failed:
            return None
        samples = [0] * sample_count
        # DS1023 covers 1023..992, followed by DS0991 covering 991..960.
        for index, block in self.blocks.items():
            for offset, value in enumerate(block):
                samples[index - offset] = value
        return tuple(samples)


def bandscope_frequency(status, index):
    """DS frequencies remain relative to CF when MF moves (manual section 17-3)."""
    if not 0 <= index < 1024:
        raise ValueError('Invalid DS sample index')
    narrow = status['span_code'] in (6, 7)
    return status['centre_hz'] + (index - (64 if narrow else 512)) * bandscope_resolution(status['span_code'])


def parse_select_scan_response(text):
    """GR lists a select-scan slot followed by the tagged memory's settings."""
    empty = re.fullmatch(r'GR([0-9]{2}) ---', text)
    if empty is not None:
        return {'slot': empty.group(1), 'channel': None}
    match = re.fullmatch(
        r'GR([0-9]{2}) MX([A-Ja-j][0-9]{2}) '
        r'RF([0-9]{10}|[0-9]{4}\.[0-9]{4,5}) '
        r'ST([0-9]{6}|[0-9]+\.[0-9]+) AU([01]) MD([0-8]) AT([01]) TM(.*)', text)
    if match is None:
        raise ValueError('Invalid Select Scan response')
    slot, channel, frequency, step, auto, mode, attenuation, name = match.groups()
    frequency_hz = Decimal(frequency) * (1000000 if '.' in frequency else 1)
    step_khz = Decimal(step) / (1 if '.' in step else 1000)
    return {'slot': slot, 'channel': channel, 'frequency_hz': frequency_hz,
            'step_khz': step_khz, 'auto': auto, 'mode': int(mode),
            'attenuation': attenuation, 'name': name}


def parse_pass_frequency_response(text):
    """PR identifies both the search-bank/VFO context and the pass-list slot."""
    match = re.fullmatch(r'PR([A-Ta-tV])([0-9]{2}) ([0-9]{10}|---)', text)
    if match is None or int(match.group(2)) > 49:
        raise ValueError('Invalid pass-frequency response')
    context, slot, frequency = match.groups()
    return {'context': context, 'slot': slot,
            'frequency_hz': None if frequency == '---' else Decimal(frequency)}


def make_pass_frequency_command(context, frequency_mhz):
    """Explicit PW context and integer Hz avoid changing the tuned frequency."""
    if not re.fullmatch(r'[A-Ta-tV]', context):
        raise ValueError('Invalid pass-frequency context')
    if not re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,6})?', frequency_mhz):
        raise ValueError('Enter MHz with up to six decimal places')
    frequency_hz = Decimal(frequency_mhz) * 1000000
    if not 100000 <= frequency_hz < 3000000000 or frequency_hz % 50:
        raise ValueError('Use 0.1 to below 3000 MHz, in 50 Hz increments')
    return ('PW%s%010d\r\n' % (context, int(frequency_hz))).encode('ascii')


def format_activity_row(activity):
    """Format the existing in-memory LC1 record for the read-only log view."""
    frequency, duration = activity['frequency_hz'], activity['duration']
    return (activity['timestamp'].strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
            '---' if frequency is None else format(frequency / 1000000, '.6f'),
            '%s %s' % (activity['source'], activity['source_id']),
            str(activity['level']), 'OPEN' if activity['squelch_open'] else 'CLOSED',
            '---' if duration is None else '%.2f s' % duration)


def parse_lm_response(text):
    """Return the raw signal level and squelch state; the final space matters."""
    match = re.fullmatch(r'LM([ %])([0-9]{3})', text)
    if match is not None:
        level = int(match.group(2))
        if level > 255:
            raise ValueError('Invalid LM response')
        return level, match.group(1) == ' '
    match = re.fullmatch(r'LM([0-9A-Fa-f]{2})([ %])', text)
    if match is None:
        raise ValueError('Invalid LM response')
    return int(match.group(1), 16), match.group(2) == ' '


def parse_lc_response(text):
    """Parse LC1 activity, whose signal level is decimal, not hexadecimal."""
    match = re.fullmatch(
        r'LC(%?)([0-9]{3}) (V[ABF]|SR[A-Ta-t]|M[A-Ta-t][0-9]{2})'
        r'(?: RF([0-9]{10}|[0-9]{4}\.[0-9]{4,5}))?', text)
    if match is None:
        raise ValueError('Invalid LC activity response')
    closed, level, context, frequency = match.groups()
    level = int(level)
    if level > 255 or (not closed and frequency is None):
        raise ValueError('Invalid LC level or missing open frequency')
    if context.startswith('V'):
        source, identifier = 'VFO', context[1:]
    elif context.startswith('SR'):
        source, identifier = 'Search', context[2:]
    else:
        source, identifier = 'Memory', context[1:]
    frequency_hz = None
    if frequency is not None:
        frequency_hz = Decimal(frequency)
        if '.' in frequency:
            frequency_hz *= 1000000
    return {'source': source, 'source_id': identifier, 'frequency_hz': frequency_hz,
            'level': level, 'squelch_open': not bool(closed)}


# noinspection PyUnusedLocal
def do_nothing(evt):
    """catch and trash mousewheel movements"""
    pass


def format_frequency(x):
    """
    RFnnnnnnnnnn ->  n,nnn.nnn,nnn
    """
    x = list(x)
    last = -1
    for idx, item in enumerate(x):
        if item == '0':
            last = idx
        else:
            break

    if last > 2:
        x = x[3:]
        x.insert(1, '.')
    else:
        x.insert(4, '.')
        x = x[last+1:]

    return ''.join(x)


def format_step(x):
    """
    STnnnnnn -> nnn.nnn khz
    minor step = 0.00005
    max step   = 0.10000
    """
    return x


def receiver_frequency_hz(value):
    """Validate new search inputs using the installed AR8600's frequency range."""
    command = bandscope_centre_command(value.strip())
    frequency = int(command[2:-2])
    if frequency % 50:
        raise ValueError('Frequency must be in 50 Hz increments')
    return frequency


def search_step_hz(value):
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value.strip()):
        raise ValueError('Enter a numeric step in kHz')
    step = Decimal(value.strip()) * 1000
    # 8.33 represents the documented eight-and-one-third airband step.
    if step != 8330 and (not 50 <= step <= 999950 or step % 50):
        raise ValueError('Use 0.05-999.95 kHz in 0.05 kHz steps, or 8.33 kHz')
    return int(step)


def search_parameters(lower, upper, step, mode_code):
    lower_hz, upper_hz = receiver_frequency_hz(lower), receiver_frequency_hz(upper)
    if lower_hz >= upper_hz:
        raise ValueError('Lower frequency must be less than upper frequency')
    step_hz = search_step_hz(step)
    if mode_code is not None and mode_code not in range(9):
        raise ValueError('Select Auto or a valid mode')
    return lower_hz, upper_hz, step_hz


def make_search_bank_command(bank, lower, upper, step, mode_code, name):
    if not re.fullmatch(r'[A-Ta-t]', bank):
        raise ValueError('Select a Search Bank A-T or a-t')
    lower_hz, upper_hz, step_hz = search_parameters(lower, upper, step, mode_code)
    if len(name) > 12 or any(not 32 <= ord(char) <= 126 for char in name):
        raise ValueError('Use at most 12 printable ASCII characters for the name')
    command = 'SE%s SL%010d SU%010d AU%d' % (bank, lower_hz, upper_hz, mode_code is None)
    if mode_code is not None:
        command += ' ST%06d MD%d' % (step_hz, mode_code)
    # AT is deliberately omitted: retain the bank's existing attenuation.
    return (command + ' TT' + name + '\r\n').encode('ascii')


def parse_search_bank_response(text):
    empty = re.fullmatch(r'SR([A-Ta-t]) ---', text)
    if empty:
        return {'bank': empty.group(1), 'empty': True}
    match = re.fullmatch(
        r'SR([A-Ta-t]) SL([0-9]{10}) SU([0-9]{10}) '
        r'ST((?:[0-9]{6}|[0-9]+\.[0-9]+)\+?) AU([01]) MD([0-8])(?: AT([01]))? TT(.*)', text)
    if not match:
        raise ValueError('Invalid Search Bank definition')
    bank, lower, upper, step, auto, mode, attenuation, name = match.groups()
    if int(lower) >= int(upper):
        raise ValueError('Invalid Search Bank limits')
    return {'bank': bank, 'empty': False, 'lower_hz': int(lower), 'upper_hz': int(upper),
            'step_khz': Decimal(step.rstrip('+')) / (1 if '.' in step else 1000),
            'step_adjust': step.endswith('+'), 'step_response': step,
            'auto': auto == '1', 'mode': int(mode), 'attenuation': attenuation, 'name': name}


def parse_group_members(text, kind):
    banks = 'ABCDEFGHIJabcdefghij' if kind == 'scan' else 'ABCDEFGHIJKLMNOPQRSTabcdefghijklmnopqrst'
    prefix = 'BM' if kind == 'scan' else 'BS'
    match = re.fullmatch(prefix + r'\s*([A-Za-z-]{%d})' % len(banks), text)
    if not match:
        raise ValueError('Invalid group membership response')
    members = match.group(1).replace('-', '')
    if any(bank not in banks for bank in members) or len(set(members)) != len(members):
        raise ValueError('Invalid or duplicate group bank')
    return tuple(bank for bank in banks if bank in members)


def make_group_members_command(kind, group, members):
    if kind not in ('scan', 'search') or not 1 <= group <= 9:
        raise ValueError('Group 0 is fixed LINK OFF; edit groups 1-9 only')
    banks = 'ABCDEFGHIJabcdefghij' if kind == 'scan' else 'ABCDEFGHIJKLMNOPQRSTabcdefghijklmnopqrst'
    if any(bank not in banks for bank in members) or len(set(members)) != len(members):
        raise ValueError('Invalid group banks')
    select, link = ('GM', 'BM') if kind == 'scan' else ('GS', 'BS')
    ordered = ''.join(bank for bank in banks if bank in members)
    return ('%s%d\r\n%s%%%%%s\r\n%s\r\n' % (select, group, link, ordered, link)).encode('ascii')
