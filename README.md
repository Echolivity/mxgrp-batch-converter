# mxgrp batch converter: Maschine kits to Ableton Live 12 Drum Racks

A safe, audited batch converter from Native Instruments Maschine `.mxgrp` kits to Ableton Live 12 Drum Rack `.adg` presets.
Based on [24407kt/mxgrp-to-drumrack](https://github.com/24407kt/mxgrp-to-drumrack) (MIT). The upstream `.adg` template engine is kept;
this project adds a rewritten `.mxgrp` parser, dry-run / report / incremental modes, validation and safety rules.

> Not affiliated with or endorsed by Native Instruments or Ableton. The `.mxgrp` format is reverse-engineered and may change.
> This repository contains no NI or Ableton content. You need your own Maschine libraries and the Ableton *Drum Essentials*
> pack (its `Wrong Sided Kit.adg` is the template). The tool never modifies or deletes your source files and never overwrites
> existing outputs unless you pass `--overwrite`. Use at your own risk. Tested on Windows with Python 3.13, Live 12 and one
> large library; see the verification and limitations sections below.

# Maschine .mxgrp -> Ableton Live 12 Drum Rack (batch, safe mode)

Built on https://github.com/24407kt/mxgrp-to-drumrack (MIT). The `.adg` generation technique (string replacement
on the Drum Essentials "Wrong Sided Kit.adg" template) is unchanged; the original generator is kept as
`generate_drum_rack_adg_legacy` for regression comparison.

## Files
| File | Purpose |
|---|---|
| `batch_convert.py` | **Run this.** Discovery, validation, dry-run/execute, manifest, CSV reports, logs |
| `mxgrp_parser.py` | Structured record parser (replaces the `.wav` substring scan) |
| `mxgrp_to_drumrack.py` | Upstream engine, patched (note-addressed generator, RIFF-walking WAV reader, validator) |
| `tests/test_regression_3onit.py` | 3onIt regression tests |
| `FORMAT_NOTES.md` | What is/isn't known about the binary format |

## What changed vs upstream
1. **Pad 10-12 rotation: root cause was the generator, not the parser.** Template branch order is not note order.
   Now chosen by `ReceivingNote`.
2. Parser reads sound records (name, instrument, sample) in serialised order; no dedup, so reused samples don't shift pads.
3. Plugin pads (Massive) are detected and reported `UNSUPPORTED_PLUGIN`; they are never silently dropped.
4. Unused template branches are removed; `Id`s renumbered 0..n-1. Pad labels are set to the Maschine pad name
   (upstream left "Kick Kindified" etc.).
5. WAV reader walks RIFF chunks (LIST/bext/extensible); truncated or corrupt files are detected.
6. Missing/corrupt sample => kit **not** written (unless `--allow-missing-samples`).
7. No silent truncation: >16 sounds => error. Fewer than 16 sounds => `REVIEW_SHORT_KIT` (see limitations).
8. Output is re-read and validated (XML parses, pad count, notes, sample refs) before it replaces anything. Writes are atomic.
9. Change detection by SHA-256 of source + template, converter/parser version, sample root and options (not mtimes),
   stored in `<output>\.mxgrp_converter_manifest.json`. Stale outputs are reported, not overwritten, without `--overwrite`.

## PowerShell guide
```powershell
cd C:\path\to\mxgrp-batch          # folder containing batch_convert.py
python --version                    # 3.8+ ; standard library only

# 0. Tests (optional)
$env:MXGRP_3ONIT    = "H:\Maschinelib\Amplified Funk Library\Groups\Kits\3onIt Kit.mxgrp"
$env:MXGRP_TEMPLATE = "D:\abpcks\Drum Essentials\Drums\Hybrid\Wrong Sided Kit.adg"
python -m unittest discover -s tests -v

# 1. Regression kit only (writes ONE .adg)
python batch_convert.py "H:\Maschinelib" "D:\NI Drum Racks" `
  --template "D:\abpcks\Drum Essentials\Drums\Hybrid\Wrong Sided Kit.adg" --execute --only "3onIt Kit" --report ".\reports"

# 2. Full-library DRY RUN (writes no .adg; only CSVs + log in .\reports)
python batch_convert.py "H:\Maschinelib" "D:\NI Drum Racks" `
  --template "D:\abpcks\Drum Essentials\Drums\Hybrid\Wrong Sided Kit.adg" --dry-run --report ".\reports"

# 3. Batch conversion (only after steps 1-2 are reviewed)
python batch_convert.py "H:\Maschinelib" "D:\NI Drum Racks" `
  --template "D:\abpcks\Drum Essentials\Drums\Hybrid\Wrong Sided Kit.adg" --execute --report ".\reports"
```
Flags: `--overwrite` (replace existing/stale outputs), `--only TEXT`, `--accept-short-kits`, `--accept-parse-warnings`,
`--allow-missing-samples`, `--no-previews`, `--no-xmp`.

## Results on the real library (converter 2.1.0, parser 3.1.0)
Full `--execute` over `H:\Maschinelib`, 5,462 `.mxgrp` files, zero errors:

| Status | Kits |
|---|---|
| CONVERTED (all 16 pads are samples) | 2,859 |
| CONVERTED_PARTIAL (sample pads converted; plugin / drum-synth pads reported, left empty) | 2,147 |
| SKIPPED_UP_TO_DATE (converted in earlier test runs) | 49 |
| SKIPPED_MULTIZONE_INSTRUMENT | 211 |
| SKIPPED_NO_SUPPORTED_PADS (no sample pads, e.g. effect groups / pure synth kits) | 177 |
| REVIEW_SHORT_KIT (fewer than 16 pad records, held) | 19 |

Total racks written: 5,055 (matches the dry-run prediction exactly).

## Verified by you against Maschine and Live 12
* 3onIt Kit: pads 10-12 = Sidestick / Tom / Vocal, rack loads in Live 12.
* Cachophony Kit and Amplified Funk "Chakra Kit" (hybrid kits with drum-synth pads between sample pads): sample pads sit
  on the same MIDI notes / pad numbers as in Maschine; synth and plugin pads are empty.
* DaOne Kit: a sample name with a double space was matched by the whitespace-normalised fallback (recorded in the kit report).

## Unit tests (Linux sandbox, real 3onIt + real template, synthetic silent WAVs)
`python -m unittest discover -s tests -v`: 9 tests, OK. They cover parser order/names/plugin, reproduction of the upstream
pad 10-12 bug versus the fix, XML escaping, dry-run writes nothing, execute + ADG verification, the pad-CSV Massive row,
second-run skip / stale handling, missing and corrupt samples, and the WAV extensible reader.

## NOT verified
* Push 2 pad layout (nothing here can run Ableton or a Push).
* Only a few racks were opened in Live; the other ~5,000 are structurally validated (XML re-parsed, branch count, notes, paths)
  but not individually auditioned.
* Pad numbering for the 19 short kits (see below) and the 4 `Groups\Kits` Rhodes kits skipped as multizone (assumed to be
  one multi-sampled pad).

## Known limitations
* **Short kits (19, held).** 16 are real kits with <16 pad records; 3 are `Multi FX` groups (not drum kits, correctly not converted).
  Where those kits' pads sit (pads 1..N?) is unverified. `--accept-short-kits` converts them assuming pads 1..N; use it only
  after checking one such kit in Maschine. Girls Make Beats Kit references a library ("Community Drive 2021") that is not
  installed, so Maschine cannot load it.
* **Multizone instruments (211, skipped).** 207 are `Groups\Loops`; 4 are Rhodes kits. A single multi-zone sampler is not a drum rack.
* **Plugin pads** (Massive, Monark, ...) are listed as `UNSUPPORTED_PLUGIN` and left empty. **Maschine drum-synth pads** are
  listed as unsupported and left empty. NI effects are ignored (not converted, do not block conversion).
* Pad -> MIDI is assumed 36 + index (Maschine default); custom note maps are not read.
* Multi-zone samplers on a normal kit use the first zone only (warned in the kit report).
* Sample names are matched exactly, then by a whitespace-normalised UNIQUE match in the same folder (noted in the report).
* Volume / pitch / envelope / effect settings are not converted. `DefaultSampleRate` stays as in the template (44.1 kHz).
* `BrowserContentPath` keeps the template's stale string (as upstream).
* Pad numbering for unusual kits relies on the name-sequence parser (see FORMAT_NOTES.md). It holds kits it is unsure about
  (`REVIEW_PARSE_UNCERTAIN`) rather than guessing. None were held in the final run.
