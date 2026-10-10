# Frequency Database CSV

Open a local UTF-8 CSV (an optional BOM is accepted) with **File -> Open Frequency
Database CSV...**. The independent **Lists -> DATABASE** view works offline.
Startup loads the configured default CSV without selecting DATABASE. If no custom
default is configured, or it cannot be loaded, it tries
`data/default_frequency_database.csv` relative to the application location.
This small bundled sample contains nine HFCC A26 transmissions with their original
schedule validity dates; it is not a complete or automatically updated database.
Refresh rereads the current local file. No downloading occurs at startup.

Normalized columns, with exact case-sensitive names:

```csv
Frequency,Station,UTCStart,UTCEnd,Days,Band,Language,Target,TxSite,TxCountry,StationCountry,PowerKW,TxAzimuth,TxLatitude,TxLongitude,Mode,Step,ValidFrom,ValidTo,Notes,Source
```

`Frequency` and `Station` headers are required. Other headers may be omitted;
missing fields are blank. Unknown columns are ignored. The complete file is
validated before replacing the displayed database. Normal CSV quoting applies,
including names/notes containing commas or newlines.

- Frequency: positive number with optional Hz/kHz/MHz units. Bare numbers mean
  MHz, matching the application's existing CSV convention. Examples:
  `6070 kHz`, `93.500000 MHz`. Browsing accepts frequencies outside the receiver
  range; tuning uses the existing receiver range and 50 Hz input validation.
- Station: descriptive station name.
- UTCStart/UTCEnd: `HH:MM` or `HHMM`; end may be `24:00`/`2400`. Both blank means
  all day; equal start/end also means all day. Start is inclusive, end exclusive.
  For a range crossing midnight, Days and validity dates apply to the date when
  that transmission started, including its part after midnight.
- Days: blank, `Daily`, `All`, or `*` means every day. Use Mon/Tue/Wed/Thu/Fri/Sat/Sun,
  separated by spaces or commas, ranges such as `Mon-Fri`, or ISO weekday digits
  `1234567` (Monday=1, Sunday=7). Source schedules using Sunday=1 must be converted
  before importing. The application does not guess source-specific conventions.
- Days `Unknown` retains an unrepresentable source schedule without claiming it
  is daily: it does not highlight as on air or pass the On air now filter. The
  original qualifier remains in Notes (for example, first Saturday or irregular).
- ValidFrom/ValidTo: optional ISO dates `YYYY-MM-DD`, inclusive.
- Band, Language, Target, TxSite, TxCountry, StationCountry, Notes, Source: text.
  Band and Language filter values come directly from the loaded CSV.
- PowerKW: optional non-negative numeric kW. The numeric cell remains visible;
  restrained backgrounds indicate `<10` light red, `10-<100` pale yellow,
  `100-<250` light green, and `>=250` green. Missing power is uncoloured.
- TxAzimuth: optional 0-360 degrees, clockwise from true north. Explicit ND,
  non-directional or omnidirectional markers are also accepted and have no beam offset.
- TxLatitude/TxLongitude: optional decimal degrees, north/east positive,
  latitude -90..90, longitude -180..180. Both are needed for geometry.
- Mode: WFM, NFM, SFM, WAM, AM, NAM, USB, LSB, or CW (case-insensitive).
- Step: optional Hz/kHz/MHz value; bare numbers mean kHz. Existing step validation
  applies, including 8.33 kHz. Mode and Step are required only for double-click tuning.

**Receiver Location...** stores coordinates in `AR8600/frequency_database.json`
under wxPython's per-user configuration
directory (normally `%APPDATA%` on Windows). This file is outside the repository.
`last_database_path` remembers the last manually opened file for the File dialog.
`default_database_path` independently selects the startup database; an absent or
empty value uses the bundled sample. Existing preference files remain compatible.
**Tools -> Frequency Database Settings...** validates and loads a custom default
before saving it, or clears that custom choice with **Use bundled database**.
**File -> Open Frequency Database CSV...** loads for the current session without
changing the default. Clearing both receiver coordinates disables geometry.

RX bearing is the great-circle initial heading from receiver to transmitter.
Distance uses a spherical Earth radius of 6371.0088 km. Beam offset is the
smallest angle between TxAzimuth and the initial transmitter-to-receiver heading,
not the difference between TxAzimuth and RX bearing. Derived values stay blank
when their required inputs are absent; coincident/antipodal headings are undefined.
The read-only DataView shows Freq, Station, UTC, Days, Lang, Tx, kW, and Bearing.
Columns support typed sorting in either direction and remain resizable, with
Freq, UTC, Days, kW and Bearing bounded to compact, font-measured widths.
Station, Tx and Lang have readable minimums and share extra width approximately
60/30/10 on resize; horizontal scrolling is needed only when the window is too
narrow for those minimums or columns are enlarged manually.
Country filters by TxCountry and combines with
Band, Language, Search and On air now. Frequency display precision is consistent
within each band and remains fixed when filtering.

UTC and Days cells are light green for schedules currently on air, respecting
UTC, weekdays, validity dates and overnight schedules, independently of the
On air now filter. Time state refreshes every 30 seconds while the view is active.
Right-click **Refresh On-Air Status** recalculates it without reloading the CSV.
Bearing-cell backgrounds show beam offset: green through 10 degrees, light green
through 25, yellow through 45, orange through 90, and light red above 90.
Missing or non-directional beam information leaves the background unchanged.
Cell tooltips show full/contextual values; the Bearing tooltip includes RX
bearing, Tx azimuth, beam offset and distance when available.

**View -> Station Details** opens a modeless window that follows the selected
DATABASE row. It displays available normalized fields and derived geometry,
including Target and StationCountry, omitting empty fields. No permanent details
panel occupies the main table area.

Double-clicking requires a connected, idle receiver and valid Frequency/Mode/Step.
It selects the main GUI's active VFO, disables Auto to apply the explicit Mode/Step,
and sends RF/MD/ST under the existing temporary MC1/MC0 protection, followed by RX.
Scanner read-back updates the main GUI. It does not recall or write memories;
attenuation is left unchanged.

## Building the combined HFCC A26 + EiBi A26 database

Run `python build_frequency_database.py` to download the
[HFCC A26 operational ZIP](https://new.hfcc.org/data/a26/a26allx2.zip),
[EiBi A26 CSV](http://eibispace.de/dx/sked-a26.csv) and
[EiBi README/code tables](http://www.eibispace.de/dx/README.TXT).
The output is `AR8600_FREQUENCY_DATABASE_A26.csv`, using the same 21 columns.
It is generated locally and should not be committed. To reproduce a build
without network access, supply `--zip`, `--eibi-csv` and `--eibi-readme` paths.

HFCC supplies all its operational records, including the rich transmitter
engineering data: power, azimuth, coordinates and resolved transmitter sites.
EiBi extends broadcast coverage beyond HFCC, including LW/MW where present,
and supplies station-country, language, target and schedule metadata. EiBi-only
rows have no invented power, coordinates or azimuth. The code resolves field 8
as transmitter-site code regardless of the misleading `Remarks` header. A blank
site code indicates the documented home transmitter country, but cannot prove
a particular site: the README also permits blanks for unknown sites.

The generator excludes EiBi `P >= 90` utilities, `P=8` inactive entries and
`P=4` winter-only entries from summer A26. Some published utility records lack
the documented `P >= 90` marker; explicit utility language codes and service
descriptions such as NAVY, VOLMET and FAX are also excluded and counted. It does
not guess service type from a call sign. Non-broadcast LF/MF regions are outside
scope; LW is 148.5-283.5 kHz and MW is 520-1710 kHz. Existing SW metre-band labels
are retained, including out-of-band SW broadcasts.

Blank Days means daily. Weekday lists/ranges convert to ISO weekdays, including
ranges wrapping Sunday. UTC endpoints are preserved, including 0000-2400 and
overnight ranges. Only `P=6` Start/Stop fields define validity dates: DDMM is
interpreted within A26 (2026), advancing the end year for a crossing-year range.
A single-date Days qualifier can narrow that documented interval. Other date
fields, last-heard `[MMYY]` markers, persistence codes, DST/season qualifiers and
unrepresentable monthly/irregular Days remain in Notes; no false validity dates
or daily schedules are manufactured.

Confirmed merging requires identical frequency, UTC start/end and weekdays,
compatible validity dates, plus either normalized station identity or uniquely
agreeing site and language metadata. Conflicting known transmitter sites or
countries prevent merging even if station names match. Site matching ignores
parenthetical reference annotations and kW labels, not geographic differences.
An undated EiBi schedule cannot absorb a
narrower HFCC validity interval. Both directions must be unique: multiple HFCC
candidates or multiple EiBi matches stay separate. Station matching uses only
spelling/abbreviation normalization and the explicit BBC/Worldservice alias,
without fuzzy matching. HFCC engineering fields are retained; EiBi fills useful
blank descriptive fields, including StationCountry. Source becomes
`HFCC A26; EiBi A26`; all uncertain/unmatched EiBi records remain separate.

EiBi-only normal broadcasts use AM, 9 kHz for LW/MW (10 kHz for explicitly
identified American transmitters), and 5 kHz for SW. Explicit USB/LSB or digital
annotations are preserved; unsupported digital modulation and unspecified steps
remain blank rather than being falsely labelled AM. Existing HFCC receiver
defaults are unchanged. Rows sort by numeric Frequency, UTCStart and Station.
Diagnostics include input/exclusion/merge totals, unresolved codes and sample
merged and EiBi-only records. A missing LW sample means none was supplied by
the downloaded sources, not that LW data was fabricated.

EiBi is compiled by Eike Bierwirth. Its README permits free download, use,
copying, redistribution and inclusion in third-party software. Credit is retained
in Source/Notes and this documentation; no additional attribution condition is
asserted. Consult the [EiBi conditions of use](http://www.eibispace.de/dx/README.TXT)
for the original terms and contact information. EiBi explicitly provides no
guarantee that individual schedules or transmitter details are correct.
