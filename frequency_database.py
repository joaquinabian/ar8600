"""Offline frequency schedules, receiver geometry and a dedicated database view."""
import csv
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import wx
import wx.dataview as dv

from aor_functions import display_step, search_step_hz


CSV_FIELDS = ('Frequency', 'Station', 'UTCStart', 'UTCEnd', 'Days', 'Band', 'Language',
              'Target', 'TxSite', 'TxCountry', 'StationCountry', 'PowerKW', 'TxAzimuth',
              'TxLatitude', 'TxLongitude', 'Mode', 'Step', 'ValidFrom', 'ValidTo', 'Notes', 'Source')
BUNDLED_DATABASE_PATH = Path(__file__).resolve().parent / 'data' / 'default_frequency_database.csv'
MODE_NAMES = ('WFM', 'NFM', 'SFM', 'WAM', 'AM', 'NAM', 'USB', 'LSB', 'CW')
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def quantity(value, default_unit):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*(Hz|kHz|MHz)?', value.strip(), re.IGNORECASE)
    if match is None:
        raise ValueError('Use a number with Hz, kHz or MHz')
    return Decimal(match[1]) * {'hz': 1, 'khz': 1000, 'mhz': 1000000}[(match[2] or default_unit).lower()]


def utc_minutes(value, end=False):
    if not value:
        return None
    match = re.fullmatch(r'([0-9]{2}):?([0-9]{2})', value)
    if match is None:
        raise ValueError('UTC times must be HH:MM or HHMM')
    hour, minute = map(int, match.groups())
    if end and (hour, minute) == (24, 0):
        return 1440
    if hour > 23 or minute > 59:
        raise ValueError('Invalid UTC time')
    return hour * 60 + minute


def schedule_days(value):
    value = value.strip().lower()
    if value in ('', '*', 'daily', 'all'):
        return frozenset(range(1, 8))
    if re.fullmatch(r'[1-7]+', value):
        return frozenset(map(int, value))  # ISO weekdays: Monday=1, Sunday=7.
    names = {name.lower(): index + 1 for index, name in enumerate(WEEKDAYS)}
    days = set()
    for token in re.split(r'[\s,;]+', value):
        if token in names:
            days.add(names[token])
        elif re.fullmatch(r'[1-7]', token):
            days.add(int(token))
        elif '-' in token:
            start, end = token.split('-', 1)
            if start not in names or end not in names:
                raise ValueError('Days must use Mon-Sun or ISO weekday numbers 1-7')
            current = names[start]
            while True:
                days.add(current)
                if current == names[end]:
                    break
                current = current % 7 + 1
        else:
            raise ValueError('Days must use Mon-Sun or ISO weekday numbers 1-7')
    return frozenset(days)


def optional_number(value, label, minimum, maximum=None):
    if not value:
        return None
    try:
        number = Decimal(value)
    except ArithmeticError as error:
        raise ValueError(label + ' must be numeric') from error
    if not number.is_finite() or number < minimum or (maximum is not None and number > maximum):
        raise ValueError('Invalid ' + label)
    return number


def transmitter_azimuth(value):
    marker = re.sub(r'[\s/-]', '', value).casefold()
    if marker in ('nd', 'nondirectional', 'omni', 'omnidirectional'):
        return None
    return optional_number(value, 'TxAzimuth', 0, 360)


@dataclass
class FrequencyRecord:
    values: dict
    frequency_hz: Decimal
    step_hz: object
    start: object
    end: object
    days: frozenset
    valid_from: object
    valid_to: object
    power_kw: object
    latitude: object
    longitude: object
    azimuth: object
    geometry: dict = field(default_factory=dict)

    def on_air(self, now):
        now = now.astimezone(timezone.utc)
        day = now.date()
        minute = now.hour * 60 + now.minute
        if self.start is not None and self.start != self.end:
            if self.start < self.end:
                if not self.start <= minute < self.end:
                    return False
            elif minute < self.end:
                day -= timedelta(days=1)
            elif minute < self.start:
                return False
        return (day.isoweekday() in self.days and
                (self.valid_from is None or day >= self.valid_from) and
                (self.valid_to is None or day <= self.valid_to))

    def utc_label(self):
        if self.start is None:
            return 'All day'
        return '%02d:%02d-%02d:%02d' % (self.start // 60, self.start % 60, self.end // 60, self.end % 60)


def load_csv(path):
    records = []
    with open(path, newline='', encoding='utf-8-sig') as source:
        reader = csv.DictReader(source, strict=True)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)) or any(not name.strip() for name in headers):
            raise ValueError('CSV headers must be non-empty and unique')
        if not {'Frequency', 'Station'}.issubset(headers):
            raise ValueError('CSV requires Frequency and Station columns')
        for line, row in enumerate(reader, 2):
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError('Incorrect column count')
                values = {name: row.get(name, '').strip() for name in CSV_FIELDS}
                frequency = quantity(values['Frequency'], 'MHz')
                if frequency <= 0:
                    raise ValueError('Frequency must be positive')
                step = search_step_hz(format(quantity(values['Step'], 'kHz') / 1000, 'f')) if values['Step'] else None
                values['Mode'] = values['Mode'].upper()
                if values['Mode'] and values['Mode'] not in MODE_NAMES:
                    raise ValueError('Unknown Mode')
                start, end = utc_minutes(values['UTCStart']), utc_minutes(values['UTCEnd'], end=True)
                if (start is None) != (end is None):
                    raise ValueError('Supply both UTCStart and UTCEnd, or leave both blank')
                days = schedule_days(values['Days'])
                valid_from = date.fromisoformat(values['ValidFrom']) if values['ValidFrom'] else None
                valid_to = date.fromisoformat(values['ValidTo']) if values['ValidTo'] else None
                if valid_from and valid_to and valid_from > valid_to:
                    raise ValueError('ValidFrom must not be after ValidTo')
                records.append(FrequencyRecord(values, frequency, step, start, end, days, valid_from, valid_to,
                    optional_number(values['PowerKW'], 'PowerKW', 0),
                    optional_number(values['TxLatitude'], 'TxLatitude', -90, 90),
                    optional_number(values['TxLongitude'], 'TxLongitude', -180, 180),
                    transmitter_azimuth(values['TxAzimuth'])))
            except (ValueError, ArithmeticError) as error:
                raise ValueError('CSV line %d: %s' % (line, error)) from error
    return records


def initial_bearing(latitude1, longitude1, latitude2, longitude2):
    lat1, lat2 = math.radians(latitude1), math.radians(latitude2)
    delta = math.radians(longitude2 - longitude1)
    x = math.sin(delta) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta)
    if abs(x) < 1e-14 and abs(y) < 1e-14:
        return None  # Coincident/antipodal points have no unique initial heading.
    return math.degrees(math.atan2(x, y)) % 360


def angular_difference(first, second):
    return abs((first - second + 180) % 360 - 180)


def receiver_geometry(location, record):
    if location is None or record.latitude is None or record.longitude is None:
        return {}
    rx_lat, rx_lon = location
    tx_lat, tx_lon = float(record.latitude), float(record.longitude)
    lat1, lat2 = math.radians(rx_lat), math.radians(tx_lat)
    haversine = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(tx_lon - rx_lon) / 2) ** 2
    distance = 6371.0088 * 2 * math.asin(math.sqrt(max(0, min(1, haversine))))
    heading = initial_bearing(rx_lat, rx_lon, tx_lat, tx_lon)
    # The reverse initial great-circle heading is not heading + 180 degrees.
    tx_heading = initial_bearing(tx_lat, tx_lon, rx_lat, rx_lon)
    offset = angular_difference(tx_heading, float(record.azimuth)) if record.azimuth is not None and tx_heading is not None else None
    return {'bearing': heading, 'distance': distance, 'beam_offset': offset}


def power_class(power):
    if power is None:
        return None
    return 0 if power < 10 else 1 if power < 100 else 2 if power < 250 else 3


def beam_offset_colour(offset):
    if offset is None:
        return None
    if offset <= 10:
        return (180, 224, 180)
    if offset <= 25:
        return (222, 241, 223)
    if offset <= 45:
        return (255, 245, 178)
    if offset <= 90:
        return (255, 216, 170)
    return (250, 205, 205)


def band_frequency_precisions(records):
    """Choose one MHz precision per band from the full, unfiltered database."""
    precisions = {}
    for record in records:
        mhz = record.frequency_hz / Decimal(1000000)
        required = max(1, -mhz.normalize().as_tuple().exponent)
        band = record.values['Band']
        precisions[band] = max(precisions.get(band, 1), required)
    return precisions


class DatabasePreferences:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else Path(wx.StandardPaths.Get().GetUserConfigDir()) / 'AR8600' / 'frequency_database.json'
        self.location, self.last_path, self.default_path = None, '', ''
        try:
            values = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(values, dict):
                raise ValueError('Settings must be a JSON object')
            if values.get('receiver_location') is not None:
                location = values['receiver_location']
                latitude = optional_number(str(location['latitude']), 'Latitude', -90, 90)
                longitude = optional_number(str(location['longitude']), 'Longitude', -180, 180)
                self.location = (float(latitude), float(longitude))
            if isinstance(values.get('last_database_path'), str):
                self.last_path = values['last_database_path']
            if isinstance(values.get('default_database_path'), str):
                self.default_path = values['default_database_path']
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, ArithmeticError) as error:
            print('Frequency database settings could not be read: %s' % error, file=sys.stderr)

    def save(self, location, last_path, default_path=None):
        if default_path is None:
            default_path = self.default_path
        values = {'receiver_location': None if location is None else dict(zip(('latitude', 'longitude'), location)),
                  'last_database_path': last_path, 'default_database_path': default_path}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as output:
                temporary = output.name
                json.dump(values, output, indent=2)
                output.write('\n')
            os.replace(temporary, self.path)
            temporary = None
            self.location, self.last_path, self.default_path = location, last_path, default_path
        finally:
            if temporary is not None:
                os.unlink(temporary)


class DatabaseSettingsDialog(wx.Dialog):
    def __init__(self, panel):
        super().__init__(panel, title='Frequency Database Settings')
        self.panel = panel
        self.path = wx.TextCtrl(self, value=panel.preferences.default_path or str(BUNDLED_DATABASE_PATH),
                                size=(500, -1))
        browse = wx.Button(self, label='Browse...')
        bundled = wx.Button(self, label='Use bundled database')
        self.message = wx.StaticText(self, label='The default loads at startup. File -> Open loads only for this session.')
        self.path.SetToolTip('Default CSV loaded at startup; using the bundled database clears the custom default.')
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(self, label='Default Database path'), 0, wx.ALL, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.path, 1, wx.RIGHT, 6)
        row.Add(browse)
        layout.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        layout.Add(bundled, 0, wx.ALL, 8)
        layout.Add(self.message, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        layout.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.SetSizerAndFit(layout)
        browse.Bind(wx.EVT_BUTTON, self.on_browse)
        bundled.Bind(wx.EVT_BUTTON, lambda event: self.path.SetValue(str(BUNDLED_DATABASE_PATH)))
        self.Bind(wx.EVT_BUTTON, self.on_accept, id=wx.ID_OK)

    def on_browse(self, event):
        path = self.path.GetValue().strip()
        dialog = wx.FileDialog(self, 'Choose default frequency database',
                               defaultDir=os.path.dirname(path), defaultFile=os.path.basename(path),
                               wildcard='CSV files (*.csv)|*.csv|All files (*.*)|*.*',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.path.SetValue(dialog.GetPath())
        finally:
            dialog.Destroy()

    def on_accept(self, event):
        if self.panel.set_default_database(self.path.GetValue().strip()):
            self.EndModal(wx.ID_OK)
        else:
            self.message.SetLabel('Could not accept this database. See the status area for details.')
            self.Layout()


class ReceiverLocationDialog(wx.Dialog):
    def __init__(self, parent, location):
        super().__init__(parent, title='Receiver Location')
        self.latitude, self.longitude = wx.TextCtrl(self), wx.TextCtrl(self)
        if location is not None:
            self.latitude.SetValue(str(location[0]))
            self.longitude.SetValue(str(location[1]))
        grid = wx.FlexGridSizer(0, 2, 6, 8)
        for label, control, tip in (('Latitude', self.latitude, 'Decimal degrees: north positive, south negative; -90 to 90.'),
                                    ('Longitude', self.longitude, 'Decimal degrees: east positive, west negative; -180 to 180.')):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 0, wx.EXPAND)
            control.SetToolTip(tip)
        grid.AddGrowableCol(1)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label='Used for antenna bearing and distance. Leave both blank to clear.'), 0, wx.ALL, 10)
        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        self.error = wx.StaticText(self, label='', size=(420, 30))
        sizer.Add(self.error, 0, wx.EXPAND | wx.ALL, 10)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, self.on_ok, id=wx.ID_OK)

    def on_ok(self, event):
        try:
            latitude, longitude = self.latitude.GetValue().strip(), self.longitude.GetValue().strip()
            if bool(latitude) != bool(longitude):
                raise ValueError('Supply both coordinates or leave both blank.')
            self.location = None if not latitude else (float(optional_number(latitude, 'Latitude', -90, 90)),
                                                       float(optional_number(longitude, 'Longitude', -180, 180)))
        except ValueError as error:
            self.error.SetLabel(str(error))
            return
        self.EndModal(wx.ID_OK)


class StationDetailsFrame(wx.Frame):
    """Modeless, read-only details; absent fields consume no display space."""
    def __init__(self, parent):
        super().__init__(parent, title='Station Details', size=(580, 600))
        self.text = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.text, 1, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

    def update_record(self, record, frequency_label=''):
        if record is None:
            self.text.ChangeValue('Select a DATABASE row to view station details.')
            return
        values, geometry = record.values, record.geometry
        angle = lambda value: '' if value is None else '%.1f\N{DEGREE SIGN}' % value
        fields = (
            ('Frequency', frequency_label), ('Station', values['Station']),
            ('UTC', record.utc_label()), ('Days', values['Days'] or 'Daily'),
            ('Band', values['Band']), ('Language', values['Language']), ('Target', values['Target']),
            ('Tx Site', values['TxSite']), ('Tx Country', values['TxCountry']),
            ('Station Country', values['StationCountry']), ('Power kW', values['PowerKW']),
            ('Tx Azimuth', angle(record.azimuth) or values['TxAzimuth']),
            ('RX Bearing', angle(geometry.get('bearing'))),
            ('Beam Offset', angle(geometry.get('beam_offset'))),
            ('Distance', '' if geometry.get('distance') is None else '%.1f km' % geometry['distance']),
            ('Tx Latitude', values['TxLatitude']), ('Tx Longitude', values['TxLongitude']),
            ('Mode', values['Mode']), ('Step', '' if record.step_hz is None else display_step(record.step_hz)),
            ('Valid From', values['ValidFrom']), ('Valid To', values['ValidTo']),
            ('Notes', values['Notes']), ('Source', values['Source']))
        self.text.ChangeValue('\n'.join('%s: %s' % (label, value) for label, value in fields if value))


class DatabaseTableModel(dv.DataViewIndexListModel):
    def __init__(self):
        super().__init__(0)
        self.records = []
        self.now = datetime.now(timezone.utc)
        self.frequency_precisions = {}

    def GetColumnCount(self):
        return 8

    def GetColumnType(self, col):
        return 'string'

    def SetValueByRow(self, value, row, col):
        return False

    def frequency_label(self, record):
        decimals = self.frequency_precisions[record.values['Band']]
        # Retain the existing kHz display below 3 MHz, including its three
        # decimal places; finer values receive enough places to remain exact.
        if record.frequency_hz < 3000000:
            value, unit, decimals = record.frequency_hz / 1000, 'kHz', max(3, decimals - 3)
        else:
            value, unit = record.frequency_hz / 1000000, 'MHz'
        return '%s %s' % (format(value, '.%df' % decimals), unit)

    def GetValueByRow(self, row, col):
        record = self.records[row]
        bearing = record.geometry.get('bearing')
        values = (self.frequency_label(record), record.values['Station'], record.utc_label(),
                  record.values['Days'] or 'Daily', record.values['Language'],
                  record.values['TxSite'] or record.values['TxCountry'],
                  '' if record.power_kw is None else format(record.power_kw.normalize(), 'f'),
                  '' if bearing is None else '%d\N{DEGREE SIGN}' % (round(bearing) % 360))
        return values[col]

    def Compare(self, item1, item2, column, ascending):
        def key(item):
            record = self.records[self.GetRow(item)]
            values = (record.frequency_hz, record.values['Station'].casefold(),
                      (record.start if record.start is not None else 0,
                       record.end if record.end is not None else 1440),
                      (record.values['Days'] or 'Daily').casefold(), record.values['Language'].casefold(),
                      (record.values['TxSite'] or record.values['TxCountry']).casefold(),
                      record.power_kw, record.geometry.get('bearing'))
            value = values[column]
            return (value is None, value)
        first, second = key(item1), key(item2)
        comparison = (first > second) - (first < second)
        return comparison if ascending else -comparison

    def GetAttrByRow(self, row, col, attr):
        if col in (2, 3) and self.records[row].on_air(self.now):
            attr.SetBackgroundColour(wx.Colour(222, 241, 223))
            attr.SetColour(wx.Colour(30, 30, 30))
            return True
        if col == 7:
            colour = beam_offset_colour(self.records[row].geometry.get('beam_offset'))
        elif col == 6:
            category = power_class(self.records[row].power_kw)
            colour = ((250, 205, 205), (255, 245, 178), (222, 241, 223), (180, 224, 180))[category] if category is not None else None
        else:
            colour = None
        if colour is None:
            return False
        attr.SetBackgroundColour(wx.Colour(*colour))
        attr.SetColour(wx.Colour(30, 30, 30))
        return True

    def reset(self, records, now=None, frequency_records=None):
        self.records = records
        self.now = now if now is not None else datetime.now(timezone.utc)
        self.frequency_precisions = band_frequency_precisions(
            records if frequency_records is None else frequency_records)
        self.Reset(len(records))


class FrequencyDatabasePanel(wx.Panel):
    def __init__(self, parent, controller, preferences=None):
        super().__init__(parent)
        self.controller = controller
        self.preferences = preferences or DatabasePreferences()
        self.records, self.path = [], ''
        self.active = False
        self.startup_database_loaded = False
        self.filter_later = None
        self.details_window = None
        self.column_fit_pending = False
        sizer = wx.BoxSizer(wx.VERTICAL)
        filters = wx.BoxSizer(wx.HORIZONTAL)
        self.on_air = wx.CheckBox(self, label='On air now')
        self.band = wx.Choice(self, choices=['All bands'], size=(110, -1))
        self.language = wx.Choice(self, choices=['All languages'], size=(125, -1))
        self.country = wx.Choice(self, choices=['All'], size=(135, -1))
        self.band.SetSelection(0)
        self.language.SetSelection(0)
        self.country.SetSelection(0)
        self.search = wx.TextCtrl(self, size=(180, -1))
        filters.Add(self.on_air, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        for label, control in (('Band', self.band), ('Language', self.language),
                               ('Country', self.country), ('Search', self.search)):
            filters.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
            filters.Add(control, 1 if control is self.search else 0, wx.RIGHT, 10)
        sizer.Add(filters, 0, wx.EXPAND | wx.ALL, 5)
        geometry = wx.BoxSizer(wx.HORIZONTAL)
        self.location_button = wx.Button(self, label='Receiver Location...')
        geometry.Add(self.location_button, 0, wx.RIGHT, 10)
        self.summary = wx.StaticText(self, label='File -> Open Frequency Database CSV...')
        geometry.Add(self.summary, 1, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(geometry, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        self.model = DatabaseTableModel()
        self.table = dv.DataViewCtrl(self, style=dv.DV_SINGLE | dv.DV_ROW_LINES | dv.DV_VERT_RULES)
        self.table.AssociateModel(self.model)
        for index, (label, width) in enumerate((('Freq', 120), ('Station', 160), ('UTC', 100), ('Days', 75),
                                               ('Lang', 65), ('Tx', 110), ('kW', 55), ('Bearing', 75))):
            self.table.AppendTextColumn(label, index, width=self.FromDIP(width),
                                        flags=dv.DATAVIEW_COL_RESIZABLE | dv.DATAVIEW_COL_SORTABLE)
        self.measure_bounded_columns()
        sizer.Add(self.table, 1, wx.EXPAND)
        self.SetSizer(sizer)
        self.on_air.SetToolTip('Show schedules active now in UTC, including days and validity dates.')
        self.band.SetToolTip('Filter by the Band supplied in the CSV.')
        self.language.SetToolTip('Filter by the Language supplied in the CSV.')
        self.country.SetToolTip('Filter by transmitter country (TxCountry), independently of station country.')
        self.search.SetToolTip('Find text across station, transmitter, target, notes and other CSV fields.')
        self.location_button.SetToolTip('Configure receiver coordinates for great-circle bearing and distance.')
        self.Bind(wx.EVT_CHECKBOX, self.on_filter, self.on_air)
        for control in (self.band, self.language, self.country):
            control.Bind(wx.EVT_CHOICE, self.on_filter)
        self.search.Bind(wx.EVT_TEXT, self.on_filter)
        self.location_button.Bind(wx.EVT_BUTTON, self.on_location)
        self.table.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self.on_selection)
        self.table.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self.on_activate)
        self.table.Bind(wx.EVT_LEFT_DCLICK, self.on_double_click)
        self.table.Bind(wx.EVT_MOTION, self.on_cell_hover)
        self.table.Bind(wx.EVT_LEAVE_WINDOW, self.on_cell_leave)
        self.table.Bind(wx.EVT_MOUSEWHEEL, self.on_cell_leave)
        self.table.Bind(dv.EVT_DATAVIEW_ITEM_CONTEXT_MENU, self.on_context_menu)
        self.table.Bind(dv.EVT_DATAVIEW_COLUMN_SORTED, self.on_column_sorted)
        self.table.Bind(wx.EVT_SIZE, self.on_table_size)
        self.clock = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda event: self.apply_filters(), self.clock)
        self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)

    def measure_bounded_columns(self):
        """Measure loaded contents once, with practical limits for compact columns."""
        samples = {0: {'145.500000 MHz'}, 2: {'23:59-24:00'}, 3: {'1234567'},
                   6: {'5000'}, 7: {'359\N{DEGREE SIGN}'}}
        if self.records:
            samples[0] = {self.model.frequency_label(record) for record in self.records}
            samples[3].update(record.values['Days'] or 'Daily' for record in self.records)
            samples[6].update(format(record.power_kw.normalize(), 'f') for record in self.records
                              if record.power_kw is not None)
        limits = {0: (90, 160), 2: (90, 110), 3: (60, 105), 6: (50, 80), 7: (70, 80)}
        self.bounded_widths = {}
        for index, (minimum, maximum) in limits.items():
            header = self.table.GetTextExtent(self.table.GetColumn(index).GetTitle()).width + self.FromDIP(26)
            contents = max(self.table.GetTextExtent(value).width for value in samples[index]) + self.FromDIP(16)
            self.bounded_widths[index] = max(self.FromDIP(minimum), header,
                                             min(self.FromDIP(maximum), contents))

    def on_table_size(self, event):
        event.Skip()
        if not self.column_fit_pending:
            self.column_fit_pending = True
            wx.CallAfter(self.fit_columns)

    def fit_columns(self):
        self.column_fit_pending = False
        if not self.table:
            return
        # Client width already excludes the native scrollbar; allow for borders.
        available = self.table.GetClientSize().width - self.FromDIP(2)
        widths = dict(self.bounded_widths)
        minimums = {1: self.FromDIP(160), 5: self.FromDIP(110), 4: self.FromDIP(65)}
        extra = max(0, available - sum(widths.values()) - sum(minimums.values()))
        additions = {1: extra * 60 // 100, 5: extra * 30 // 100}
        additions[4] = extra - sum(additions.values())
        for index, minimum in minimums.items():
            widths[index] = minimum + additions[index]
        for index, width in widths.items():
            column = self.table.GetColumn(index)
            minimum = minimums.get(index, self.bounded_widths.get(index))
            if column.GetMinWidth() != minimum:
                column.SetMinWidth(minimum)
            if column.GetWidth() != width:
                column.SetWidth(width)

    def status(self, message, error=False):
        self.controller.aor_status.SetStatusText(message)
        if error:
            print(message, file=sys.stderr)

    def open_csv(self, path, remember=True):
        try:
            records = load_csv(path)
        except (OSError, ValueError, UnicodeError, csv.Error) as error:
            self.status('Frequency database could not be opened: %s' % error, error=True)
            return False
        self.show_database(records, path)
        if remember:
            try:
                self.preferences.save(self.preferences.location, self.path)
            except OSError as error:
                self.status('Loaded %d database rows; last path could not be saved: %s' % (len(records), error), error=True)
        return True

    def show_database(self, records, path):
        self.records, self.path = records, os.path.abspath(path)
        for record in records:
            record.geometry = receiver_geometry(self.preferences.location, record)
        for choice, field_name, all_label in ((self.band, 'Band', 'All bands'),
                                            (self.language, 'Language', 'All languages'),
                                            (self.country, 'TxCountry', 'All')):
            choice.SetItems([all_label] + sorted({record.values[field_name] for record in records if record.values[field_name]}, key=str.casefold))
            choice.SetSelection(0)
        self.apply_filters()
        self.measure_bounded_columns()
        self.fit_columns()
        self.update_clock()
        self.startup_database_loaded = True
        self.status('Frequency database: %d rows loaded from %s' % (len(records), self.path))

    def load_startup_database(self):
        if self.startup_database_loaded:
            return
        self.startup_database_loaded = True
        paths = [self.preferences.default_path] if self.preferences.default_path else []
        if str(BUNDLED_DATABASE_PATH) not in paths:
            paths.append(str(BUNDLED_DATABASE_PATH))
        for path in paths:
            if self.open_csv(path, remember=False):
                return
        self.status('No startup frequency database could be loaded; DATABASE remains empty.', error=True)

    def set_default_database(self, path):
        path = os.path.abspath(path or BUNDLED_DATABASE_PATH)
        try:
            records = load_csv(path)
            default_path = '' if Path(path).resolve() == BUNDLED_DATABASE_PATH else path
            self.preferences.save(self.preferences.location, self.preferences.last_path, default_path)
        except (OSError, ValueError, UnicodeError, csv.Error) as error:
            self.status('Default frequency database could not be saved: %s' % error, error=True)
            return False
        self.show_database(records, path)
        return True

    def on_database_settings(self, event):
        dialog = DatabaseSettingsDialog(self)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def set_active(self, active):
        self.active = active
        if active:
            self.load_startup_database()
            self.apply_filters()
            self.fit_columns()
        self.update_clock()

    def update_clock(self):
        if self.active and self.path:
            self.clock.Start(30000)
        else:
            self.clock.Stop()

    def on_filter(self, event):
        if self.filter_later is not None:
            self.filter_later.Stop()
        self.filter_later = wx.CallLater(150, self.apply_filters)
        self.update_clock()

    def selected_record(self):
        item = self.table.GetSelection()
        if not item.IsOk():
            return None
        row = self.model.GetRow(item)
        return self.model.records[row] if 0 <= row < len(self.model.records) else None

    def apply_filters(self):
        if self.filter_later is not None:
            self.filter_later.Stop()
        self.filter_later = None
        previous = self.selected_record()
        search = self.search.GetValue().strip().casefold()
        band = self.band.GetStringSelection() if self.band.GetSelection() > 0 else None
        language = self.language.GetStringSelection() if self.language.GetSelection() > 0 else None
        country = self.country.GetStringSelection() if self.country.GetSelection() > 0 else None
        now = datetime.now(timezone.utc)
        rows = []
        for record in self.records:
            if band is not None and record.values['Band'] != band or language is not None and record.values['Language'] != language:
                continue
            if country is not None and record.values['TxCountry'] != country:
                continue
            if search and search not in ' '.join(record.values.values()).casefold():
                continue
            if self.on_air.IsChecked() and not record.on_air(now):
                continue
            rows.append(record)
        self.table.UnsetToolTip()
        self.model.reset(rows, now, frequency_records=self.records)
        # Resetting filtered rows must retain the native column sort and direction.
        if self.table.GetSortingColumn() is not None:
            self.model.Resort()
        self.summary.SetLabel('%d / %d stations' % (len(rows), len(self.records)) if self.path else 'File -> Open Frequency Database CSV...')
        self.summary.SetToolTip(self.path)
        self.show_details(None)
        for row, record in enumerate(rows):
            if record is previous:
                self.table.Select(self.model.GetItem(row))
                self.show_details(record)
                break

    def show_details(self, record):
        if self.details_window is not None:
            self.details_window.update_record(record, '' if record is None else self.model.frequency_label(record))

    def open_station_details(self, event=None):
        if self.details_window is None:
            self.details_window = StationDetailsFrame(self.controller)
            self.details_window.Bind(wx.EVT_CLOSE, self.on_details_close)
        self.show_details(self.selected_record())
        self.details_window.Show()
        self.details_window.Raise()

    def on_details_close(self, event):
        window, self.details_window = self.details_window, None
        if window:
            window.Destroy()

    def cell_tooltip(self, row, column, width):
        record = self.model.records[row]
        value = self.model.GetValueByRow(row, column)
        if column == 7:
            geometry = record.geometry
            lines = []
            for label, number in (('RX bearing', geometry.get('bearing')),
                                  ('Tx azimuth', record.azimuth),
                                  ('Beam offset', geometry.get('beam_offset'))):
                if number is not None:
                    lines.append('%s: %.1f\N{DEGREE SIGN}' % (label, number))
                elif label == 'Tx azimuth' and record.values['TxAzimuth']:
                    lines.append('Tx azimuth: ' + record.values['TxAzimuth'])
            if geometry.get('distance') is not None:
                lines.append('Distance: %.0f km' % geometry['distance'])
            return '\n'.join(lines)
        if not value:
            return ''
        if column == 2:
            return 'UTC schedule: %s; %s.' % (value, 'on air now' if record.on_air(self.model.now) else 'not on air now')
        if column == 3:
            return 'UTC days: ' + ', '.join(WEEKDAYS[day - 1] for day in sorted(record.days))
        if column == 6:
            return 'Transmitter power: %s kW.' % value
        if column == 5 and record.values['TxSite'] and record.values['TxCountry']:
            return 'Transmitter: %s (%s)' % (record.values['TxSite'], record.values['TxCountry'])
        if column == 1 and record.values['StationCountry']:
            return '%s (%s)' % (value, record.values['StationCountry'])
        return value if self.table.GetTextExtent(value).width > max(0, width - 12) else ''

    def on_cell_hover(self, event):
        item, column = self.table.HitTest(event.GetPosition())
        tip = self.cell_tooltip(self.model.GetRow(item), column.GetModelColumn(), column.GetWidth()) if item.IsOk() and column is not None else ''
        if tip != self.table.GetToolTipText():
            self.table.SetToolTip(tip) if tip else self.table.UnsetToolTip()
        event.Skip()

    def on_cell_leave(self, event):
        self.table.UnsetToolTip()
        event.Skip()

    def on_column_sorted(self, event):
        self.table.UnsetToolTip()
        event.Skip()

    def on_context_menu(self, event):
        menu = wx.Menu()
        try:
            action = menu.Append(wx.ID_ANY, 'Refresh On-Air Status')
            menu.Bind(wx.EVT_MENU, lambda event: self.apply_filters(), id=action.GetId())
            self.table.PopupMenu(menu)
        finally:
            menu.Destroy()

    def on_selection(self, event):
        self.show_details(self.selected_record())

    def on_activate(self, event):
        if event.GetItem().IsOk():
            self.controller.tune_database_record(self.model.records[self.model.GetRow(event.GetItem())])

    def on_double_click(self, event):
        item, column = self.table.HitTest(event.GetPosition())
        if item.IsOk() and column is not None and column.GetModelColumn() != 0:
            self.controller.tune_database_record(self.model.records[self.model.GetRow(item)])
        else:
            event.Skip()  # Primary column uses the native activation event.

    def on_location(self, event):
        dialog = ReceiverLocationDialog(self, self.preferences.location)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            try:
                self.preferences.save(dialog.location, self.preferences.last_path)
            except OSError as error:
                self.status('Receiver location could not be saved: %s' % error, error=True)
                return
        finally:
            dialog.Destroy()
        for record in self.records:
            record.geometry = receiver_geometry(self.preferences.location, record)
        self.apply_filters()
        self.status('Receiver location saved.' if self.preferences.location is not None else 'Receiver location cleared.')

    def on_destroy(self, event):
        if event.GetEventObject() is self:
            self.clock.Stop()
            if self.filter_later is not None:
                self.filter_later.Stop()
            window, self.details_window = self.details_window, None
            if window and not window.IsBeingDeleted():
                window.Destroy()
        event.Skip()
