__author__ = 'joaquin'

import re
from decimal import Decimal


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
