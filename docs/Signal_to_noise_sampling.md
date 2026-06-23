# Silent-input detection — sampling & threshold calibration

History of why the silent-input detector in the Android recording
path is computed the way it is. Useful when someone wonders why
we poll at 25 ms instead of 100 ms, or why the multiplier is 27
instead of a rounder number.

## Problem

After a recording, the recorder needs to tell whether the
captured audio actually contained the user's voice. The encoder
produces a correctly-sized M4A file even when the mic input is
silent, so the post-stop file-size check passes; we need a
separate amplitude-based check.

The original heuristic was a fixed-constant detector:

```
silent = (max_amp < 100)  # _SILENT_AMP_THRESHOLD
```

`max_amp` came from polling `MediaRecorder.getMaxAmplitude()`
every 500 ms during the recording. It worked on the development
phone, where ambient silence read ~40 and real speech read in
the thousands.

It broke on a second device — a tablet whose ambient AC-noise
floor read ~380, well above the fixed threshold. A genuinely
silent recording on that tablet would *never* trip the detector
because the noise floor alone exceeded the threshold.

## Hypotheses we considered

Five candidate failure modes for the original symptom ("toast
fires every recording on user's Redmi Note 15 Pro+ 5G even
though sometimes the audio is fine"):

1. `getMaxAmplitude()` permanently broken on that AOSP build.
2. Threshold mis-calibrated for that mic's gain.
3. Main-thread starvation making the 500 ms poll miss speech.
4. Audio routing to a connected BT/USB peripheral.
5. Android 12+ Mic-off privacy toggle.

Disambiguating these added the `polls`, `zeros`, `mute`, and
`mic=` diagnostic fields to the silent-input toast (still
present in production for the field-debug path) — see the
`[record] stop:` log line and the toast suffix in
`main.py` (`_stop_record_worker_android` and
`_publish_stop_finish`).

## Why adaptive instead of a re-tuned fixed threshold

Any fixed threshold trades one device's silent-detection
correctness against another's. The tablet's noise floor (380)
sat above the phone's typical whisper minimum and below the
phone's normal speech, leaving no constant that works for both.
We needed something that adapted per-device per-recording.

## Iterations

### 1.55.26 — `min_nz × 4`

First attempt: use the smallest non-zero per-poll amplitude as
a noise-floor proxy, with a multiplier for headroom:

```
threshold = max(100, 4 × min_amp_nonzero)
```

Reasoning: the quietest 100 ms window of a recording should be
close to the noise floor; multiplying by 4 gives margin over it.

**Why it didn't work.** At 100 ms polling, almost every
recording — silent, whisper, normal speech — has at least one
quiet inter-syllable window where `getMaxAmplitude` returns 1-
20. So `min_nz` saturated near zero across every scenario, and
`4 × min_nz` was always below the floor of 100, so the adaptive
rule never engaged.

Captured in field-user tests:

| Scenario | max | min_nz |
|---|---|---|
| Door-closed handover, hall noise | 243 | 4 |
| Same, second take | 450 | 2 |
| Real speech | 23 543 | 4 |

`min_nz` is essentially a constant near zero regardless of what
the recording actually contained. It doesn't measure noise
floor; it measures "the quietest 100 ms moment that happened
to be sampled."

### 1.55.27 — percentile instrumentation

Added per-poll amplitude capture and computed
p5/p10/p20/p30/p40/median/mean alongside `min_nz`. Detection
behavior unchanged; the new fields were diagnostic only.
Polled at 50 ms to get 20-40 samples on a typical recording.

### 1.55.28 — 100 ms A/B

Flipped polling from 50 ms to 100 ms, otherwise unchanged,
to see how coarser sampling affected the percentiles.
(Skipped in favour of the combined 25/50/100 ms approach
in 1.55.29.)

### 1.55.29 — 25 ms native + coalesced views

Polled natively at 25 ms; the stop worker reported percentile
stats at n=1 (25 ms), n=2 (50 ms-equivalent), and n=4 (100 ms-
equivalent) coalesced views. Mathematically identical to having
polled separately at each rate, because
`max(max(w1), max(w2)) = max over (w1 ∪ w2)`, and a single run
of each scenario captured all three rates' data at once with no
between-session noise.

## Calibration data

Eight simultaneous recordings — phone + tablet, both
equidistant from the user's mouth, four scenarios each.

Labels per the user testing this:

- **Silent (should trigger):** phone#1 (max=37), tablet#1
  (max=3764), tablet#2 (max=4614, a "low whisper" tablet
  picked up but user wouldn't want to keep).
- **Pass (should not trigger):** phone#2 (max=1339, low
  whisper kept), phone#3-5, tablet#3-5.

### Percentile choice — why median

At the 1.55.27 50 ms calibration:

| Statistic | Silent range | Pass range | Gap ratio | Undefined cases |
|---|---|---|---|---|
| p5 | 6.5 – 115 | 12k+ (3 undef) | overlap | 3 |
| p10 | 3.4 – 7.9 | 23.6 – 3806 | 3.0× | 1 |
| p20 | 2.4 – 4.4 | 10.0 – 74.6 | 2.3× | 0 |
| p30 | 2.2 – 3.8 | 8.7 – 66.9 | 2.3× | 0 |
| p40 | 1.9 – 3.4 | 8.3 – 64.5 | 2.4× | 0 |
| **med** | **1.7 – 3.0** | **7.6 – 50.7** | **2.5×** | 0 |

Median wins on three fronts: largest gap ratio, no undefined
cases, and intuitive ("typical amplitude during the recording").
p5/p10 saturate at zero in scenarios where most of the
recording is loud (real speech), so the `max / pN` ratio blows
up or is undefined — brittle. p20/p30/p40 work but are tighter.

### Sampling rate choice — why 25 ms

Repeated the same scenarios at 25 / 50 / 100 ms (1.55.29). Gap
ratios using `max / median`:

| Rate | max silent ratio | min pass ratio | gap |
|---|---|---|---|
| 25 ms | 16.0 (tablet#2) | 44.6 (phone#2) | **2.79×** |
| 50 ms | 14.0 | 34.5 | 2.46× |
| 100 ms | 12.0 | 21.6 | 1.80× |

**Why narrower windows give better SNR.** Smaller per-poll
windows are *more* likely to land entirely inside an inter-
syllable quiet gap, so the median is more likely to reflect
the actual noise floor. Wider windows guarantee that every
bucket catches some speech, so the median measures
signal-to-signal rather than signal-to-noise. This is the
underlying reason 100 ms collapses the gap — phone#4 at 100 ms
has ratio 21.6 because its 100 ms median is 348 (every window
caught speech) while at 25 ms the median is 97. Same
recording, very different "noise floor" reading.

100 ms is rejected on principle (signal-to-signal). 25 ms beats
50 ms by ~13 % headroom (2.79× vs 2.46×) at ~0.1 % extra CPU.

### Multiplier choice — why 27

At 25 ms the silent-pass boundary lies between 16.0 (max
silent ratio) and 44.6 (min pass ratio). For equal
*proportional* margin on both sides, use the geometric mean:

```
sqrt(16.0 × 44.6) = sqrt(713.6) ≈ 26.7 → 27
```

At threshold ratio 27:
- Silent margin: 27 / 16.0 = 1.69× — silent cases are 69 %
  below threshold.
- Pass margin: 44.6 / 27 = 1.65× — pass cases are 65 % above
  threshold.

Equal headroom, no asymmetric bias.

Validation with the closest-to-boundary cases at 25 ms:

| Case | max | med | 27 × med (threshold) | result | margin |
|---|---|---|---|---|---|
| Tablet#2 silent | 4614 | 288 | 7776 | silent ✓ | 69 % below |
| Phone#2 low-whisper pass | 1339 | 30 | 810 | pass ✓ | 65 % above |

## 1.55.30 production rule + the linearity bug it had

Initial production rule:

```python
silent_threshold = max(_SILENT_AMP_FLOOR, _SILENT_MED_RATIO * med)
silent = max_amp < silent_threshold
```

This worked on all eight calibration cases but degenerated in
production at high median values. A field user's good recording
hit `max=30582 / med=2036`; the rule computed
`27 × 2036 = 54972`, which **exceeds the 16-bit PCM amplitude
cap of 32767**, so `max_amp < 54972` was vacuously true and
silent fired on every recording with `med > 1214`.

The deeper issue isn't the math wall — it's that the linear
extrapolation past calibration was unsupported. All calibration
silent cases had `med ≤ 353`; nothing in the dataset justified
the rule's behaviour above that. The new data point sat in the
untested regime, and a "high median" recording is physically a
sustained signal — not the spike-on-quiet-background pattern
that the ratio rule is designed to discriminate.

## 1.56.2 production rule — adds an explicit max ceiling

```python
stats = self._poll_amp_stats(poll_amps or [])
med = stats['med']
if med is None or med == 0:
    silent_threshold = self._SILENT_AMP_FLOOR  # 100
else:
    silent_threshold = max(
        self._SILENT_AMP_FLOOR,
        self._SILENT_MED_RATIO * med)  # 27 × med

silent = (record_ok and held > 1.0
          and max_amp < silent_threshold
          and max_amp < self._SILENT_MAX_CEILING)  # 7000
```

The ceiling at 7000 sits 52 % above the calibration's worst
silent max (4614, tablet#2) and well below the lowest pass max
where the ratio rule was doing the work (phone#4 at 7525).
Above 7000, the recording is loud enough that we trust it's
real audio regardless of what the median says, and the ratio
rule (which has no data support past the calibration regime)
no longer gets to fire.

### Why a ceiling instead of a non-linear ratio curve

The user pointed out that we'd been treating the rule as
linear from `med=0` up to `med=1214` (the math cliff at
`32767/27`), with no physical justification. Two options:

1. Replace the linear `27 × med` with a non-linear curve
   that saturates as med grows.
2. Keep the linear rule but only apply it where it's data-
   supported, falling back to "pass" outside that region.

We chose (2). Option 1 would require either more calibration
data covering the high-med region or arbitrary curve choices
(sqrt, log, etc.) without empirical grounding. Option 2 is
honest about what the data shows: "the ratio rule works
between max = 0 and max = 4614 (the worst calibration silent
case); above that we have no silent observations and assume
real audio." A future field report of a silent recording with
max > 7000 would prompt re-tuning the ceiling, not
re-shaping the ratio curve.

The floor still wins for degenerate inputs (no polls landed,
median is zero) and for very low-median recordings where 27 ×
med would otherwise be too aggressive on real speech that
happens to have a quiet first window. `held > 1.0` short-
circuits silent detection for taps — short audio is rejected
upstream as tap-not-hold.

## Code references

- `main.py:_SILENT_AMP_FLOOR` (= 100), `_SILENT_MED_RATIO` (= 27),
  `_SILENT_MAX_CEILING` (= 7000)
- `main.py:_poll_amp_stats` — sorted percentile + mean helper
- `main.py:_poll_recording` — 25 ms tick, accumulates `_poll_amps`
- `main.py:_stop_record_worker_android` — computes
  `silent_threshold` and emits the `[record] stop:` log line
- `main.py:_publish_stop_finish` — toast diagnostic suffix

## When to revisit this

- A field report of a silent recording with `max_amp > 7000`
  → raise `_SILENT_MAX_CEILING`. We currently have zero
  silent observations above max=4614; if a new device or
  environment produces one, the ceiling needs to move and the
  failure case should be added to the calibration table.
- A field report of a false-positive silent toast on legitimate
  speech with `max_amp ≤ 7000` → the ratio rule is too
  aggressive for that scenario. Capture the `[record] stop:`
  line and add it to the calibration table; the multiplier may
  need to drop or the function may need to be made non-linear
  in the low-max regime.
- A new device class with a very different noise-floor / gain
  profile shows up and one of the calibration ratios moves
  out of the (16, 44.6) window we picked 27 inside. Either
  device-keyed multipliers or a wider safety floor would be
  the next move.
- The poll rate could drop back to 50 ms if the 25 ms ticks
  ever start causing Kivy main-thread issues (none observed at
  this writing). 50 ms reduces the gap ratio to 2.46× but is
  still workable.
