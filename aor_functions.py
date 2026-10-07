__author__ = 'joaquin'

import re
from decimal import Decimal


BANDSCOPE_SPANS = {1: 10000000, 2: 5000000, 3: 2000000, 4: 1000000,
                   5: 500000, 6: 200000, 7: 100000}


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
    def __init__(self):
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
        if index in self.blocks:
            if self.blocks[index] != values:
                self.failed = True
                raise ValueError('Conflicting duplicate DS block')
            return None  # Identical duplicates neither count nor overwrite.
        self.blocks[index] = values
        if index == 31:
            self.finished = True
            if len(self.blocks) != 32:
                self.failed = True
                raise ValueError('Incomplete DS sweep: %d / 32 blocks' % len(self.blocks))
        if len(self.blocks) != 32 or self.failed:
            return None
        samples = [0] * 1024
        # DS1023 covers 1023..992, followed by DS0991 covering 991..960.
        for index, block in self.blocks.items():
            for offset, value in enumerate(block):
                samples[index - offset] = value
        return tuple(samples)


def bandscope_frequency(status, index):
    """Use the manual's centre indices, without extrapolating its edge table."""
    if not 0 <= index < 1024:
        raise ValueError('Invalid DS sample index')
    if status['marker_hz'] != status['centre_hz']:
        raise ValueError('Manual does not establish geometry for a moved marker')
    narrow = status['span_code'] in (6, 7)
    return status['centre_hz'] + (index - (64 if narrow else 512)) * (2000 if narrow else 10000)


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
