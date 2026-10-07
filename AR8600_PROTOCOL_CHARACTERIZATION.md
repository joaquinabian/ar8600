Completed on COM14 at 19200 baud, 8N2, XON/XOFF enabled, RTS/CTS disabled, timeout 1 second. No application code changed and no serial exceptions occurred.

[Complete raw transcript](C:/Users/joaquin/AppData/Local/Temp/ar8600_protocol_1ky3g_fh.log) contains every command, returned line, trailing space, acknowledgement and timing.

**1. TB bank-list behavior**

The three requests returned **first ten → second ten → first ten**. Requests 1 and 3 were byte-for-byte identical.

Requests 1 and 3:
```python
b'MW A:70 TBAFM RADIO\r\n'
b'MW a:30 TBa        \r\n'
b'MW B:70 TBBMW RADIO\r\n'
b'MW b:30 TBb        \r\n'
b'MW C:50 TBC        \r\n'
b'MW c:50 TBc        \r\n'
b'MW D:90 TBDCB      \r\n'
b'MW d:10 TBdair band\r\n'
b'MW E:50 TBErepeater\r\n'
b'MW e:50 TBeair band\r\n'
```

Request 2:
```python
b'MW F:60 TBFSERVICES\r\n'
b'MW f:40 TBf        \r\n'
b'MW G:50 TBGTAXIS   \r\n'
b'MW g:50 TBgair band\r\n'
b'MW H:70 TBHmarine  \r\n'
b'MW h:30 TBhair band\r\n'
b'MW I:70 TBIISS     \r\n'
b'MW i:30 TBi        \r\n'
b'MW J:80 TBJ6M BCNS \r\n'
b'MW j:20 TBjPMR VHF \r\n'
```

**No final blank acknowledgement was received for any TB request.**

**2. MA memory-list behavior**

| Command sequence | Returned channels |
|---|---|
| First `MAA` | A00–A09 |
| Second `MAA` | A00–A09, identical responses |
| `MRA10`, then `MA` | A10–A19 |
| `MRA50`, then `MA` | **A20–A29**, not A50–A59 |
| Three additional `MA` requests | A30–A39, A40–A49, A50–A59 |

Observed behavior: `MAA` restarts the bank-A listing at A00. Subsequent bare `MA` requests advance through blocks independently of memory recall. Both `MRA10` and `MRA50` returned only `b'\r\n'` acknowledgements.

The A50–A59 block was:
```python
b'MXA50 MP0 RF0106600000 ST100000 AU0 MD0 AT1 TMradio estel \r\n'
b'MXA51 MP0 RF0106900000 ST100000 AU0 MD0 AT0 TMradio canal \r\n'
b'MXA52 MP0 RF0107500000 ST100000 AU0 MD0 AT0 TMcultura fm  \r\n'
b'MXA53 MP0 RF0107700000 ST100000 AU0 MD0 AT0 TMradio gracia\r\n'
b'MXA54 ---\r\n'
b'MXA55 ---\r\n'
b'MXA56 ---\r\n'
b'MXA57 ---\r\n'
b'MXA58 ---\r\n'
b'MXA59 ---\r\n'
```

Thus, MA explicitly identifies empty channels as **`MXAxx ---`**. They are neither omitted nor represented by question marks. MA responses begin with `MX`, without the `MR ` prefix used by RX in memory state. No final blank acknowledgement appeared in these MA listings.

All other returned MA lines are recorded verbatim in the linked transcript.

**3. Receive latency**

Five RX queries were measured with each method, using the same timeout and receiving the same A50 status response.

First-byte and complete-line times are host-observed milliseconds from initiating the write. The last column measures the read call’s duration.

| Method | Query | First byte | Complete line | Read call duration |
|---|---:|---:|---:|---:|
| `readlines()` | 1 | 11.1 | 43.2 | 1045.2 |
| | 2 | 19.7 | 51.6 | 1060.1 |
| | 3 | 13.4 | 45.5 | 1052.1 |
| | 4 | 15.1 | 47.1 | 1052.8 |
| | 5 | 16.2 | 48.1 | 1047.2 |
| `read_until(b'\n')` | 1 | 6.7 | 39.0 | 38.8 |
| | 2 | 15.5 | 47.5 | 47.4 |
| | 3 | 15.8 | 47.9 | 47.7 |
| | 4 | 15.7 | 47.8 | 47.7 |
| | 5 | 15.7 | 47.7 | 47.6 |

Complete responses arrived in approximately **39–52 ms**. `readlines()` returned approximately **one second later**, while `read_until()` returned immediately upon receiving LF. Timeout remained 1 second throughout; no application optimization was made.

The initial and restored RX responses matched exactly:
```python
b'VB RF0093500000 ST100000 AU1 MD0 AT1\r\n'
```

COM14 closed cleanly. No stored memories were altered, and nothing was committed or pushed.
