# AR8600 Memory Bank CSV

Use **File → Import Memory Bank CSV...** or **File → Export Memory Bank CSV...**.
Only CSV is supported. No database, `.mem`, or XLSX conversion is performed.

Required headers, with exact spelling:

```text
Bank,Channel,Frequency,Step,Auto,Mode,Att,Skip,Selected,Name
```

Exports also include `BankName` and `Capacity`, repeated on populated rows.
An empty bank has one summary row with these three metadata fields and empty
channel fields. Capacity is informational; importing never resizes a bank.
Headers may appear in any order. Files use UTF-8; exports include a UTF-8 BOM
for spreadsheet compatibility. Normal CSV quoting applies.

- Channel: `A02`, `a02`, or `02`. The selected target bank overrides the source
  bank letter. Duplicate channel numbers after this override are rejected.
- Frequency: an explicit Hz/kHz/MHz value, such as `93.500000 MHz` or
  `500.000 kHz`. Bare numbers mean MHz. The existing receiver frequency range
  and 50 Hz resolution validation apply.
- Step: an explicit Hz/kHz/MHz value; bare numbers mean kHz. Existing tuning
  step validation applies, including the special `8.33 kHz` step.
- Auto, Att, Skip, Selected: `0` or `1`.
- Mode: WFM, NFM, AM, USB, LSB, CW, SFM, WAM, or NAM.
- Name: up to twelve printable ASCII characters.
- BankName: optional, at most eight printable ASCII characters. All rows must
  agree. If omitted, the current bank label is preserved; an explicitly empty
  BankName clears the label.

Import supports **Replace bank** only. The confirmation shows target, current
and imported labels, populated entry count, capacity and overwrite mode.
All target-bank channels are cleared, and imported rows become its complete
content. Other banks are preserved. A header-only file represents an empty
replacement bank. Protected individual channels are not automatically unlocked;
a failed clear stops the import.

Every file is validated before scanner writes. Writes are followed by channel
read-back, then a full-bank/Selected-membership verification. All eight fields,
including Auto's returned Mode and Step, must match; no local bandplan is used.
An error stops further memory writes and attempts verified receiver/protection
restoration. Already written changes are not rolled back. A lost connection
can prevent restoration verification and is reported explicitly.

Unknown columns, for example `DuplexPair`, `Role`, or `Use`, are retained in
memory by target bank/channel after a successful import and included on bank
export during this application run. They are never sent to the scanner and
are not persisted between runs.

Before import, stop scan/search and bandscope. If the receiver is recalling a
channel in the target bank, switch to a VFO first: clearing that channel cannot
preserve its original recall context. Temporary flag edits use the existing
forced-squelch/recall/restore sequence.

Protocol references: [AR8600 manual, section 17-3](https://www.aor.tokyo/support/discontinued/ar8600_manual.pdf).
`MX` writes, `MQx%%` clears only bank x, `TBx` sets its label, `WP` queries/sets
global protection, and `WM` queries/sets bank protection. `TB`/`MA`/`GR` supply
verification; `GA`/`MP` update Selected/Skip in a validated recall context.
