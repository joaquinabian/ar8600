__author__ = 'joaquin'

import re
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP


BANDSCOPE_SPANS = {1: 10000000, 2: 5000000, 3: 2000000, 4: 1000000,
                   5: 500000, 6: 200000, 7: 100000}
BANDSCOPE_SPAN_LABELS = ('10 MHz', '5 MHz', '2 MHz', '1 MHz', '500 kHz', '200 kHz', '100 kHz')
BANDSCOPE_AVERAGE_WINDOWS = (1, 2, 5, 10, 20, 50, 100)
BANDSCOPE_MIN_FREQUENCY_HZ = 100000
BANDSCOPE_MAX_FREQUENCY_HZ = 2040000000


@dataclass(frozen=True)
class BandscopeRunConfig:
    mode: str
    span_code: int
    centre_hz: int | None
    marker_hz: int | None
    threshold: int
    average_window: int
    average_scale: str


class BandscopeReceiverState:
    """Only validated receiver read-backs belong here; GUI edits are separate."""
    GEOMETRY = (('centre_hz', 'CF%010d'), ('span_code', 'SW%d'), ('marker_hz', 'MF%010d'))

    def __init__(self):
        self.known = {}

    def invalidate(self, *fields):
        for field in fields or tuple(self.known):
            self.known.pop(field, None)

    def confirm(self, status):
        for field in ('centre_hz', 'span_code', 'marker_hz', 'peak_hold'):
            if field in status:
                self.known[field] = status[field]

    def changes(self, desired):
        return b''.join(((pattern % desired[field]) + '\r\n').encode('ascii')
                        for field, pattern in self.GEOMETRY
                        if desired.get(field) is not None and self.known.get(field) != desired[field])

    def writing(self, data):
        # A write is not confirmation, including writes from other GUI paths.
        for line in data.split(b'\r\n'):
            for prefix, field in ((b'CF', 'centre_hz'), (b'SW', 'span_code'),
                                  (b'MF', 'marker_hz'), (b'PH', 'peak_hold')):
                if line.startswith(prefix) and len(line) > len(prefix):
                    self.invalidate(field)


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


class BandscopeStatistic:
    """Per-bin activity over complete DS sweeps; 0/1 never enter denominators."""
    def __init__(self, threshold=2):
        self.set_threshold(threshold)

    def clear(self):
        self.active = []
        self.valid = []
        self.sweeps = 0
        self.geometry = None

    def set_threshold(self, threshold):
        if type(threshold) is not int or not 2 <= threshold <= 15:
            raise ValueError('Statistic threshold must be 2..15')
        self.threshold = threshold
        self.clear()

    def add_sweep(self, samples, status):
        # Validate everything before mutating counters, including geometry.
        if (len(samples) not in (128, 1024) or
                any(type(level) is not int or not 0 <= level <= 15 for level in samples)):
            raise ValueError('Statistic requires a complete valid DS sweep')
        span = status['span_code']
        if span not in BANDSCOPE_SPANS or (len(samples) == 128 and span not in (6, 7)):
            raise ValueError('DS sample count does not match bandscope span')
        geometry = (status['centre_hz'], span, len(samples))
        if geometry != self.geometry:
            self.clear()
            self.geometry = geometry
            self.active = [0] * len(samples)
            self.valid = [0] * len(samples)
        for index, level in enumerate(samples):
            if level >= 2:
                self.valid[index] += 1
                if level >= self.threshold:
                    self.active[index] += 1
        self.sweeps += 1
        return self.percentages()

    def percentages(self):
        if len(self.active) != len(self.valid):
            raise ValueError('Statistic active/valid bin counts differ')
        result = []
        for index, (active, valid) in enumerate(zip(self.active, self.valid)):
            if (type(active) is not int or type(valid) is not int or
                    not 0 <= active <= valid <= self.sweeps):
                raise ValueError('Invalid Statistic counters in bin %d: active=%r, valid=%r, sweeps=%r' %
                                 (index, active, valid, self.sweeps))
            percentage = 100 * active / valid if valid else None
            if percentage is not None and not 0.0 <= percentage <= 100.0:
                raise ValueError('Statistic activity outside 0..100 in bin %d' % index)
            result.append(percentage)
        return tuple(result)


class BandscopeAverage:
    """Rolling per-bin raw means, with invalid 0/1 measurements excluded."""
    def __init__(self, window=10):
        self.set_window(window)

    def clear(self):
        self.history = deque()
        self.sums = []
        self.valid = []
        self.geometry = None
        self.values = self.relative_values = ()

    def set_window(self, window):
        if type(window) is not int or window not in BANDSCOPE_AVERAGE_WINDOWS:
            raise ValueError('Invalid Spectrum Average window')
        self.window = window
        self.clear()

    def add_sweep(self, samples, status):
        if (len(samples) not in (128, 1024) or
                any(type(level) is not int or not 0 <= level <= 15 for level in samples)):
            raise ValueError('Spectrum Average requires a complete valid DS sweep')
        span = status['span_code']
        if span not in BANDSCOPE_SPANS or (len(samples) == 128 and span not in (6, 7)):
            raise ValueError('DS sample count does not match bandscope span')
        geometry = (status['centre_hz'], span, len(samples))
        if geometry != self.geometry:
            self.clear()
            self.geometry = geometry
            self.sums = [0] * len(samples)
            self.valid = [0] * len(samples)
        if len(self.history) == self.window:
            for index, level in enumerate(self.history.popleft()):
                if level >= 2:
                    self.sums[index] -= level
                    self.valid[index] -= 1
        samples = tuple(samples)
        self.history.append(samples)
        for index, level in enumerate(samples):
            if level >= 2:
                self.sums[index] += level
                self.valid[index] += 1
        self.values = tuple(total / count if count else None
                            for total, count in zip(self.sums, self.valid))
        maximum = max((value for value in self.values if value is not None), default=None)
        self.relative_values = tuple(100 * (value / maximum) if value is not None else None
                                     for value in self.values)
        return self.values

    def averages(self, relative=False):
        # Both representations are cached at sweep completion, never recomputed on paint/zoom.
        return self.relative_values if relative else self.values


class BandscopeView:
    """Display-only bounds and transforms, independent of scanner/trace state."""
    def __init__(self):
        self.geometry = None
        self.statistic = False
        self.frequency_low, self.frequency_high = 0, 1
        self.y_low, self.y_high = 0, 15

    def reset_y(self, statistic):
        self.statistic = bool(statistic)
        self.y_low, self.y_high = 0, 100 if statistic else 15

    def reset(self, status, statistic):
        self.reset_y(statistic)
        self.geometry = None if status is None else (status['centre_hz'], status['span_hz'])
        if status is not None:
            self.frequency_low = status['centre_hz'] - status['span_hz'] / 2
            self.frequency_high = status['centre_hz'] + status['span_hz'] / 2

    def sync(self, status, statistic):
        geometry = None if status is None else (status['centre_hz'], status['span_hz'])
        if geometry != self.geometry or bool(statistic) != self.statistic:
            self.reset(status, statistic)

    def x_at(self, frequency, rect):
        left, top, right, bottom = rect
        return left + round((frequency - self.frequency_low) * (right - left) /
                            (self.frequency_high - self.frequency_low))

    def y_at(self, value, rect):
        left, top, right, bottom = rect
        return bottom - round((value - self.y_low) * (bottom - top) / (self.y_high - self.y_low))

    def data_at(self, point, rect):
        left, top, right, bottom = rect
        x, y = point
        return (self.frequency_low + (x - left) * (self.frequency_high - self.frequency_low) / (right - left),
                self.y_low + (bottom - y) * (self.y_high - self.y_low) / (bottom - top))

    def zoom(self, start, end, rect):
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            return False
        points = [(max(left, min(right, x)), max(top, min(bottom, y))) for x, y in (start, end)]
        if abs(points[0][0] - points[1][0]) < 5 or abs(points[0][1] - points[1][1]) < 5:
            return False
        first, second = (self.data_at(point, rect) for point in points)
        frequency_low, frequency_high = sorted((first[0], second[0]))
        y_low, y_high = sorted((first[1], second[1]))
        if frequency_low >= frequency_high or y_low >= y_high:
            return False  # Extremely deep zoom must not collapse floating-point bounds.
        self.frequency_low, self.frequency_high = frequency_low, frequency_high
        self.y_low, self.y_high = y_low, y_high
        return True

    def y_ticks(self):
        if not self.statistic and (self.y_low, self.y_high) == (0, 15):
            return (0, 2, 5, 10, 15)
        return tuple(self.y_low + n * (self.y_high - self.y_low) / 4 for n in range(5))


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
            '---' if frequency is None else display_frequency(frequency),
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
        r'LC(%?)([0-9]{3}) (V[ABF]|SR[A-Ta-t]|MX?[A-Ta-t][0-9]{2})'
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
        source, identifier = 'Memory', context[2:] if context.startswith('MX') else context[1:]
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


def protocol_frequency_hz(value):
    value = str(value)
    return Decimal(value) * (1000000 if '.' in value else 1)


def display_vfo_frequency(hz):
    """Readable VFO units, retaining every significant Hz digit in the label."""
    hz = Decimal(str(hz))
    if hz < 3000000:
        divisor, unit, minimum = Decimal(1000), 'kHz', 1
    elif hz < 1000000000:
        divisor, unit, minimum = Decimal(1000000), 'MHz', 3
    else:
        divisor, unit, minimum = Decimal(1000000000), 'GHz', 6
    number = format(hz / divisor, 'f')
    integer, _, fraction = number.partition('.')
    fraction = fraction.rstrip('0').ljust(minimum, '0')
    return '%s.%s %s' % (integer, fraction, unit)


def display_frequency(hz):
    hz = Decimal(str(hz))
    return ('%.3f kHz' % (hz / 1000)) if hz < 3000000 else ('%.6f MHz' % (hz / 1000000))


def display_step(hz):
    hz = Decimal(str(hz))
    value, unit = (hz, 'Hz') if hz < 1000 else (hz / 1000, 'kHz')
    return '%s %s' % (format(value.normalize(), 'f'), unit)


def protocol_step_hz(value):
    value = str(value).rstrip('+')
    return Decimal(value) * (1000 if '.' in value else 1)


def parse_level_squelch_response(text):
    match = re.fullmatch(r'DB[ +]?([0-9]{3})', text)
    if match is None or int(match.group(1)) > 255:
        raise ValueError('Invalid DB level squelch response')
    return int(match.group(1))


# XM is a protocol-code selector, not an index in the main GUI mode list.
# The English XM table repeats WFM at code 7; WAM is the inferred correction
# consistent with its nine supported scan modes. XM7 needs specific hardware confirmation.
SCAN_FILTER_MODES = ('All', 'WFM', 'NFM', 'AM', 'USB', 'LSB', 'CW', 'SFM', 'WAM', 'NAM')
SCAN_FILTER_CODES = ('F', '0', '1', '2', '3', '4', '5', '6', '7', '8')


def parse_operation_parameter(text):
    match = re.fullmatch(r'([DXS][ABDP]|XM)[ +]?([0-9.]+|FF|F)', text)
    if match is None:
        raise ValueError('Invalid scan/search parameter response')
    key, raw = match.groups()
    if key == 'XM':
        if raw not in SCAN_FILTER_CODES:
            raise ValueError('Invalid mode filter')
        value = raw
    elif key[1] in 'AB':
        if not re.fullmatch(r'[0-9]{3}', raw) or int(raw) > 255:
            raise ValueError('Invalid squelch threshold')
        value = int(raw)
    elif key[1] == 'D':
        if raw == 'FF' and key != 'XD':
            value = 'Hold'
        elif re.fullmatch(r'[0-9]\.[0-9]', raw):
            value = int(Decimal(raw) * 10)
        elif re.fullmatch(r'[0-9]{2}', raw):
            value = int(raw)
        else:
            raise ValueError('Invalid resume delay')
    else:
        if not re.fullmatch(r'[0-9]{2}', raw):
            raise ValueError('Invalid maximum dwell')
        value = int(raw)
    return key, value


def operation_parameter_command(key, value):
    if key == 'XM':
        if value not in SCAN_FILTER_CODES:
            raise ValueError('Invalid mode filter')
        raw = value
    elif key[1] in 'AB':
        if not isinstance(value, int) or not 0 <= value <= 255:
            raise ValueError('Threshold must be Off or 1-255')
        raw = '%03d' % value
    elif key[1] == 'D':
        if value == 'Hold' and key in ('DD', 'SD'):
            raw = 'FF'
        elif isinstance(value, int) and 0 <= value <= 99:
            raw = '%02d' % value
        else:
            raise ValueError('Resume delay must be Off, 0.1-9.9 s, or supported Hold')
    elif key[1] == 'P' and isinstance(value, int) and 0 <= value <= (99 if key == 'XP' else 60):
        raw = '%02d' % value
    else:
        raise ValueError('Maximum dwell must be Off or 1-%d s' % (99 if key == 'XP' else 60))
    if key not in ('DA', 'DB', 'DD', 'DP', 'XA', 'XB', 'XD', 'XM', 'XP', 'SA', 'SB', 'SD', 'SP'):
        raise ValueError('Unknown scan/search parameter')
    return (key + raw + '\r\n').encode('ascii')


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


def make_memory_channel_command(channel, frequency, auto, mode, step, attenuation, name):
    """AR8600 MX: RF and TM are mandatory; omit MD/ST for scanner Auto."""
    if re.fullmatch(r'[A-Ja-j][0-9]{2}', channel) is None:
        raise ValueError('Choose a memory channel A00-J99 or a00-j99')
    frequency_hz = receiver_frequency_hz(frequency)
    if mode not in range(9):
        raise ValueError('Choose a valid modulation mode')
    if len(name) > 12 or any(not 32 <= ord(char) <= 126 for char in name):
        raise ValueError('Use at most 12 printable ASCII characters for the name')
    command = 'MX%s RF%010d AU%d' % (channel, frequency_hz, bool(auto))
    if not auto:
        command += ' ST%06d MD%d' % (search_step_hz(step), mode)
    return (command + ' AT%d TM%s\r\n' % (bool(attenuation), name.ljust(12))).encode('ascii')


def memory_channel_read_command(channel):
    """MA starts at 00; advance only as far as the requested channel's block."""
    if re.fullmatch(r'[A-Ja-j][0-9]{2}', channel) is None:
        raise ValueError('Invalid memory channel')
    return ('MA%s\r\n' % channel[0] + 'MA\r\n' * (int(channel[1:]) // 10)).encode('ascii')


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


def group_members_replacement(kind, members):
    """Explicit replacement: a separator only when there are members."""
    link = 'BM' if kind == 'scan' else 'BS'
    joined = ''.join(members)
    return (link + '%%' + (' ' + joined if joined else '') + '\r\n').encode('ascii')


def make_group_members_command(kind, group, members):
    if kind not in ('scan', 'search') or not 1 <= group <= 9:
        raise ValueError('Group 0 is fixed LINK OFF; edit groups 1-9 only')
    banks = 'ABCDEFGHIJabcdefghij' if kind == 'scan' else 'ABCDEFGHIJKLMNOPQRSTabcdefghijklmnopqrst'
    if any(bank not in banks for bank in members) or len(set(members)) != len(members):
        raise ValueError('Invalid group banks')
    select, link = ('GM', 'BM') if kind == 'scan' else ('GS', 'BS')
    ordered = ''.join(bank for bank in banks if bank in members)
    return (('%s%d\r\n' % (select, group)).encode('ascii') +
            group_members_replacement(kind, ordered) + (link + '\r\n').encode('ascii'))
