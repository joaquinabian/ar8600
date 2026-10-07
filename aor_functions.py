__author__ = 'joaquin'

import re
from decimal import Decimal


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
