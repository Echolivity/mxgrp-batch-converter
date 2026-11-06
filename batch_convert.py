#!/usr/bin/env python3
"""
Safe batch converter: Maschine .mxgrp kits -> Ableton Live 12 Drum Rack .adg

  python batch_convert.py "H:\\Maschinelib" "D:\\NI Drum Racks" --template "<Wrong Sided Kit.adg>" --dry-run
  python batch_convert.py "H:\\Maschinelib" "D:\\NI Drum Racks" --template "<Wrong Sided Kit.adg>" --execute

Exactly one of --dry-run / --execute is required (there is no implicit default).
NI source files are only ever opened for reading.
"""

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import shutil
import sys

import mxgrp_to_drumrack as g
import mxgrp_parser as P

MANIFEST_NAME = '.mxgrp_converter_manifest.json'
SKIP_DIR_NAMES = {'.previews', 'ableton folder info', 'ni drum racks', 'drum rack presets'}

# ── kit statuses ──
CONVERTED = 'CONVERTED'
CONVERTED_PARTIAL = 'CONVERTED_PARTIAL'            # written; some pads not representable
WOULD_CONVERT = 'WOULD_CONVERT'
WOULD_CONVERT_PARTIAL = 'WOULD_CONVERT_PARTIAL'
UP_TO_DATE = 'SKIPPED_UP_TO_DATE'
EXISTS_UNMANAGED = 'SKIPPED_EXISTS_UNMANAGED'
STALE = 'STALE_NOT_OVERWRITTEN'
REVIEW_SHORT = 'REVIEW_SHORT_KIT'
REVIEW_PARSE = 'REVIEW_PARSE_UNCERTAIN'
MISSING = 'SKIPPED_MISSING_SAMPLES'
NOTHING = 'SKIPPED_NO_SUPPORTED_PADS'
MULTIZONE = 'SKIPPED_MULTIZONE_INSTRUMENT'
TOO_MANY = 'ERROR_TOO_MANY_PADS'
E_PARSE = 'ERROR_PARSE'
E_VALID = 'ERROR_VALIDATION'
E_COLLIDE = 'ERROR_OUTPUT_COLLISION'
E_WRITE = 'ERROR_WRITE'

# ── pad results ──
P_OK = 'CONVERTED'
P_PLUGIN = 'UNSUPPORTED_PLUGIN'
P_MISSING = 'MISSING_SAMPLE'
P_INVALID = 'INVALID_SAMPLE'
P_FORMAT = 'UNSUPPORTED_SAMPLE_FORMAT'
P_EMPTY = 'EMPTY'
P_UNREC = 'NO_SAMPLE_OR_PLUGIN_RECORD'
P_SYNTH = 'UNSUPPORTED_DRUM_SYNTH'
P_DROPPED = 'NOT_CONVERTED_KIT_SKIPPED'


def is_inside(child, parent):
    """True if child is parent or below it. Different Windows drives -> False (no exception)."""
    try:
        c, p = os.path.normcase(os.path.abspath(child)), os.path.normcase(os.path.abspath(parent))
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


# ───────────────────────── discovery ─────────────────────────

def discover(lib_root, output_root, only=None):
    out_real = os.path.realpath(output_root)
    found = []
    for root, dirs, files in os.walk(lib_root):
        dirs[:] = sorted(d for d in dirs
                         if d.lower() not in SKIP_DIR_NAMES
                         and os.path.realpath(os.path.join(root, d)) != out_real)
        for fn in sorted(files):
            if fn.lower().endswith('.mxgrp') and not fn.startswith('._'):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, lib_root)
                if only and only.lower() not in rel.lower():
                    continue
                found.append(full)
    return found


def _norm_name(n):
    return ' '.join(n.replace('\u00a0', ' ').split()).lower()


def resolve_sample(path):
    """-> (path_or_None, note). Exact file first; otherwise a UNIQUE match in the same folder
    that differs only by whitespace (repeated/non-breaking spaces). Never guesses beyond that."""
    if os.path.isfile(path):
        return path, ''
    d, base = os.path.split(path)
    try:
        hits = [f for f in os.listdir(d) if _norm_name(f) == _norm_name(base)]
    except OSError:
        return None, ''
    if len(hits) == 1 and os.path.isfile(os.path.join(d, hits[0])):
        return os.path.join(d, hits[0]), f'whitespace-normalised match: "{base}" -> "{hits[0]}"'
    return None, ''


def locate(lib_root, mxgrp_path):
    """-> (expansion_name, expansion_root, output_relative_path_without_ext)"""
    rel_parts = os.path.relpath(mxgrp_path, lib_root).split(os.sep)
    gi = next((i for i, p in enumerate(rel_parts[:-1]) if p.lower() == 'groups'), None)
    if gi is None:
        exp_idx = 0 if len(rel_parts) > 1 else None
        if exp_idx is None:
            exp_root, exp_name, sub = lib_root, os.path.basename(os.path.normpath(lib_root)), []
        else:
            exp_name = rel_parts[0]
            exp_root = os.path.join(lib_root, exp_name)
            sub = rel_parts[1:-1]
    else:
        if gi == 0:
            exp_root = lib_root
            exp_name = os.path.basename(os.path.normpath(lib_root))
        else:
            exp_root = os.path.join(lib_root, *rel_parts[:gi])
            exp_name = rel_parts[gi - 1]
        sub = rel_parts[gi + 1:-1]
        if sub and sub[0].lower() == 'kits':
            sub = sub[1:]
    stem = os.path.splitext(rel_parts[-1])[0]
    return exp_name, exp_root, os.path.join(exp_name, *sub, stem)


# ───────────────────────── per-kit analysis ─────────────────────────

class PadRow:
    def __init__(self, n, midi):
        self.n, self.midi = n, midi
        self.name = self.itype = self.sample_ref = self.resolved = ''
        self.validation = self.result = ''
        self.abs_path = ''
        self.frames = 0


class KitResult:
    def __init__(self, expansion, kit, source, output):
        self.expansion, self.kit, self.source, self.output = expansion, kit, source, output
        self.status = ''
        self.pads = []
        self.notes = []
        self.detected = 0
        self.converted = 0
        self.missing = []
        self.unsupported = []
        self.xml = None
        self.expected = []


def analyse_kit(mxgrp_path, expansion, exp_root, kit_name, output, opts):
    kr = KitResult(expansion, kit_name, mxgrp_path, output)
    try:
        kp = P.parse_mxgrp(mxgrp_path)
    except Exception as e:                                   # noqa
        kr.status = E_PARSE
        kr.notes.append(f'parse failure: {e}')
        return kr

    kr.detected = len(kp.sounds)
    kr.notes.extend(kp.errors)
    kr.notes.extend(kp.warnings)
    kr.notes.extend(kp.info)
    if not kp.sounds:
        kr.status = E_PARSE
        kr.notes.append('no sound records found')
        return kr
    if len(kp.sounds) > g.MAX_PADS:
        kr.status = TOO_MANY
        kr.notes.append(f'{len(kp.sounds)} pad records > {g.MAX_PADS}; refusing to guess which are real')
    nonempty = [x for x in kp.sounds if x.kind != 'empty']
    if len(nonempty) == 1 and nonempty[0].kind == 'sampler' and nonempty[0].zone_paths:
        kr.status = MULTIZONE
        kr.notes.append(f'single multi-sample instrument ("{nonempty[0].name}", {1 + len(nonempty[0].zone_paths)} samples); '
                        'not a drum kit, not converted')
        return kr

    for s in kp.sounds[:g.MAX_PADS + 4]:
        row = PadRow(s.index, g.FIRST_MIDI_NOTE + s.index - 1)
        row.name = s.name
        if s.kind == 'plugin':
            row.itype = 'Plugin: ' + (s.plugin_name or s.plugin_id)
            row.sample_ref = ''
            row.validation = 'n/a'
            row.result = P_PLUGIN
            kr.unsupported.append(f'pad {s.index} {s.name} ({s.plugin_name or s.plugin_id})')
        elif s.kind == 'sampler' and s.sample_rel:
            row.itype = 'Sampler' + (f' ({1 + len(s.zone_paths)} distinct zone samples)' if s.zone_paths else '')
            row.sample_ref = s.sample_rel
            p = os.path.join(exp_root, *s.sample_rel.split('/'))
            if s.zone_paths:
                kr.notes.append(f'pad {s.index}: multiple different sample zones, only the first is used')
            if not s.sample_rel.lower().endswith(P.SUPPORTED_EXTS):
                row.validation, row.result = 'unsupported format', P_FORMAT
                kr.unsupported.append(f'pad {s.index} {s.name} ({os.path.splitext(s.sample_rel)[1]})')
            elif resolve_sample(p)[0] is None:
                row.validation, row.result = 'MISSING', P_MISSING
                kr.missing.append(s.sample_rel)
            else:
                p, fuzzy_note = resolve_sample(p)
                if fuzzy_note:
                    kr.notes.append(f'pad {s.index}: {fuzzy_note}')
                row.resolved = p
                try:
                    info = g.read_wav_info(p)
                    row.frames = info['frames']
                    row.validation = (f"OK {info['sample_rate']}Hz {info['channels']}ch "
                                      f"{info['bits']}bit {info['frames']} frames")
                    row.result = P_OK
                    row.abs_path = p.replace('\\', '/')
                except g.WavError as e:
                    row.validation, row.result = f'INVALID: {e}', P_INVALID
                    kr.missing.append(f'{s.sample_rel} ({e})')
        elif s.kind == 'sampler':
            row.itype, row.validation, row.result = 'Sampler (no sample)', 'n/a', P_EMPTY
        elif s.kind == 'synth':
            row.itype = 'Maschine drum synth (' + ', '.join(c for c in s.classes if P.is_synth_class(c)) + ')'
            row.validation, row.result = 'n/a', P_SYNTH
            kr.unsupported.append(f'pad {s.index} {s.name} (drum synth)')
        elif s.kind == 'empty':
            row.itype, row.validation, row.result = 'Empty (default "Sound N")', 'n/a', P_EMPTY
        else:
            row.itype = 'Non-sample pad'
            row.validation, row.result = 'no sample/plugin record found (synth or other instrument)', P_UNREC
            kr.unsupported.append(f'pad {s.index} {s.name} (non-sample)')
        for w in s.warnings:
            kr.notes.append(w)
        kr.pads.append(row)

    if kr.status:                                            # TOO_MANY already set
        return kr

    # safety gates --------------------------------------------------------
    if kp.errors:
        kr.status = E_PARSE
        kr.notes.extend(kp.errors)
        return kr
    if kp.warnings and not opts.accept_parse_warnings:
        kr.status = REVIEW_PARSE
        kr.notes.append('not converted: parser reported uncertainty (see warnings); '
                        'use --accept-parse-warnings only after checking this kit in Maschine')
        return kr
    if len(kp.sounds) < g.MAX_PADS and not opts.accept_short_kits:
        kr.status = REVIEW_SHORT
        kr.notes.append(f'only {len(kp.sounds)} pad records (kit has fewer than 16). Assuming they occupy pads '
                        f'1-{len(kp.sounds)} is UNVERIFIED; check one such kit in Maschine, then use --accept-short-kits')
        return kr
    if kr.missing and not opts.allow_missing_samples:
        kr.status = MISSING
        kr.notes.append('not converted: missing/invalid samples (use --allow-missing-samples to omit those pads)')
        for r in kr.pads:
            if r.result == P_OK:
                r.result = P_DROPPED
        return kr

    good = [r for r in kr.pads if r.result == P_OK]
    if not good:
        kr.status = NOTHING
        return kr
    kr.converted = len(good)
    kr.expected = [(r.midi, r.abs_path) for r in good]
    pads = [(r.midi, r.name, r.abs_path, r.frames,
             os.path.splitext(os.path.basename(r.abs_path))[0]) for r in good]
    try:
        kr.xml = g.generate_drum_rack_adg(f'{expansion} - {kit_name}', pads, opts.template)
        problems = g.validate_adg_xml(kr.xml, kr.expected)
    except Exception as e:                                   # noqa
        kr.status = E_VALID
        kr.notes.append(f'generation failed: {e}')
        return kr
    if problems:
        kr.status = E_VALID
        kr.notes.extend('validation: ' + p for p in problems)
        kr.xml = None
        return kr
    partial = kr.converted != len(kr.pads)
    kr.status = (WOULD_CONVERT_PARTIAL if partial else WOULD_CONVERT)
    return kr


# ───────────────────────── output / manifest ─────────────────────────

def fingerprint(src_sha, tpl_sha, exp_root, opts):
    return {
        'source_sha256': src_sha,
        'template_sha256': tpl_sha,
        'converter_version': g.CONVERTER_VERSION,
        'parser_version': P.PARSER_VERSION,
        'sample_root': exp_root.replace('\\', '/'),
        'options': {'allow_missing_samples': bool(opts.allow_missing_samples),
                    'accept_short_kits': bool(opts.accept_short_kits)},
    }


def diff_fp(old, new):
    ch = [k for k in new if old.get(k) != new[k]]
    return ', '.join(ch)


def write_adg_atomic(xml, out_path, expected, overwrite):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + f'.tmp{os.getpid()}'
    try:
        with open(tmp, 'wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as gz:
                gz.write(xml.encode('utf-8'))
        # re-read what was actually written and validate it
        with gzip.open(tmp, 'rb') as f:
            back = f.read().decode('utf-8')
        problems = g.validate_adg_xml(back, expected)
        if problems:
            raise ValueError('; '.join(problems))
        if os.path.exists(out_path) and not overwrite:
            raise FileExistsError(out_path)
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def copy_preview(mxgrp_path, out_root, exp_name, rel_out):
    ogg = os.path.join(os.path.dirname(mxgrp_path), '.previews', os.path.basename(mxgrp_path) + '.ogg')
    if os.path.isfile(ogg):
        dst = os.path.join(out_root, 'Ableton Folder Info', 'Previews', rel_out + '.adg.ogg')
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(ogg, dst)


# ───────────────────────── main ─────────────────────────

KIT_COLS = ['Expansion', 'Kit', 'Source path', 'Output path', 'Conversion status',
            'Detected pads', 'Converted pads', 'Missing samples', 'Unsupported instruments',
            'Errors and warnings']
PAD_COLS = ['Expansion', 'Kit', 'Maschine pad number', 'MIDI note', 'Pad display name',
            'Instrument type', 'Sample reference', 'Resolved sample path',
            'Sample validation result', 'Conversion result']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('library')
    ap.add_argument('output')
    ap.add_argument('--template', required=True, help='Ableton "Wrong Sided Kit.adg"')
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='analyse + validate, write NO .adg files')
    mode.add_argument('--execute', action='store_true', help='write .adg files')
    ap.add_argument('--overwrite', action='store_true', help='replace existing/stale outputs')
    ap.add_argument('--report', default='mxgrp_reports', help='folder for CSV reports + log (default ./mxgrp_reports)')
    ap.add_argument('--only', help='only kits whose relative path contains this text (case-insensitive)')
    ap.add_argument('--accept-short-kits', action='store_true',
                    help='convert kits with <16 pad records, assuming they occupy pads 1..N (verify in Maschine first)')
    ap.add_argument('--accept-parse-warnings', action='store_true')
    ap.add_argument('--allow-missing-samples', action='store_true',
                    help='write the rack without pads whose sample is missing/invalid')
    ap.add_argument('--no-previews', action='store_true')
    ap.add_argument('--no-xmp', action='store_true')
    opts = ap.parse_args(argv)

    lib = os.path.abspath(opts.library)
    out_root = os.path.abspath(opts.output)
    if not os.path.isdir(lib):
        ap.error(f'library not found: {lib}')
    if not os.path.isfile(opts.template):
        ap.error(f'template not found: {opts.template}')
    if is_inside(opts.report, lib):
        ap.error('--report folder must not be inside the NI library')
    if is_inside(lib, out_root):
        ap.error('output folder must not contain the NI library')

    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    rep = os.path.abspath(opts.report)
    os.makedirs(rep, exist_ok=True)
    log_path = os.path.join(rep, f'run_{stamp}.log')
    logging.basicConfig(force=True, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(log_path, encoding='utf-8'),
                                  logging.StreamHandler(sys.stdout)])
    log = logging.getLogger('mxgrp')
    log.info('mode=%s overwrite=%s library=%s output=%s', 'EXECUTE' if opts.execute else 'DRY-RUN',
             opts.overwrite, lib, out_root)
    log.info('converter=%s parser=%s', g.CONVERTER_VERSION, P.PARSER_VERSION)

    try:
        g.load_template(opts.template)
        notes = set(g.template_branch_map(opts.template))
    except Exception as e:                                   # noqa
        log.error('template unusable: %s', e)
        return 2
    want = {g.midi_to_receiving_note(g.FIRST_MIDI_NOTE + i) for i in range(g.MAX_PADS)}
    if notes != want:
        log.error('template branches %s do not cover notes 36..51 as expected', sorted(notes))
        return 2
    tpl_sha = sha256_file(opts.template)

    manifest_path = os.path.join(out_root, MANIFEST_NAME)
    manifest = {}
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)

    kits = discover(lib, out_root, opts.only)
    log.info('discovered %d .mxgrp files', len(kits))

    results, claimed = [], {}
    for n, path in enumerate(kits, 1):
        exp_name, exp_root, rel_out = locate(lib, path)
        kit = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(out_root, rel_out + '.adg')
        key = os.path.relpath(path, lib).replace('\\', '/')

        if out_path.lower() in claimed:
            kr = KitResult(exp_name, kit, path, out_path)
            kr.status = E_COLLIDE
            kr.notes.append(f'same output path as {claimed[out_path.lower()]}')
            results.append((kr, key, rel_out, None))
            continue
        claimed[out_path.lower()] = path

        kr = analyse_kit(path, exp_name, exp_root, kit, out_path, opts)
        fp = None
        if kr.status in (WOULD_CONVERT, WOULD_CONVERT_PARTIAL):
            fp = fingerprint(sha256_file(path), tpl_sha, exp_root, opts)
            exists = os.path.isfile(out_path)
            entry = manifest.get(key)
            if exists and not opts.overwrite:
                if entry and entry.get('output') == out_path.replace('\\', '/'):
                    d = diff_fp(entry.get('fingerprint', {}), fp)
                    if d:
                        kr.status = STALE
                        kr.notes.append(f'existing output is out of date (changed: {d}); not overwritten')
                    else:
                        kr.status = UP_TO_DATE
                else:
                    kr.status = EXISTS_UNMANAGED
                    kr.notes.append('output already exists and was not created by this tool; not overwritten')
                kr.xml = None
            elif exists and opts.overwrite and opts.dry_run:
                kr.notes.append('WOULD OVERWRITE existing output')

        if opts.execute and kr.status in (WOULD_CONVERT, WOULD_CONVERT_PARTIAL):
            try:
                write_adg_atomic(kr.xml, out_path, kr.expected, opts.overwrite)
                kr.status = CONVERTED if kr.status == WOULD_CONVERT else CONVERTED_PARTIAL
                manifest[key] = {'output': out_path.replace('\\', '/'), 'fingerprint': fp,
                                 'output_sha256': sha256_file(out_path),
                                 'converted_at': dt.datetime.now().isoformat(timespec='seconds')}
                if not opts.no_previews:
                    copy_preview(path, out_root, exp_name, rel_out)
            except FileExistsError:
                kr.status = EXISTS_UNMANAGED
                kr.notes.append('output appeared during run; not overwritten')
            except Exception as e:                           # noqa
                kr.status = E_WRITE if not isinstance(e, ValueError) else E_VALID
                kr.notes.append(f'write/validation failed: {e}')
        kr.xml = None
        results.append((kr, key, rel_out, fp))
        log.info('[%d/%d] %-26s %s', n, len(kits), kr.status, key)

    # ── metadata (execute only) ──
    if opts.execute:
        os.makedirs(out_root, exist_ok=True)
        tmpm = manifest_path + '.tmp'
        with open(tmpm, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, indent=1, sort_keys=True)
        os.replace(tmpm, manifest_path)
        if not opts.no_xmp:
            afi = os.path.join(out_root, 'Ableton Folder Info')
            items = []
            for kr, key, rel_out, _ in results:
                if os.path.isfile(os.path.join(out_root, rel_out + '.adg')) and kr.status in (
                        CONVERTED, CONVERTED_PARTIAL, UP_TO_DATE, STALE):
                    items.append((rel_out.replace(os.sep, '/') + '.adg',
                                  g.classify_genre(kr.expansion), 'Hybrid Kit'))
            if items:
                os.makedirs(afi, exist_ok=True)
                cfg = os.path.join(afi, 'Properties.cfg')
                if not os.path.isfile(cfg):
                    with open(cfg, 'w') as f:
                        f.write('Ableton#04I\n\nFolderConfigData\n{\n'
                                '  String PackUniqueID = "ni-expansions-drumrack";\n'
                                '  String PackDisplayName = "NI Drum Racks";\n'
                                '  String PackVendor = "";\n  Int PackMinorVersion = 1;\n'
                                '  Int PackMajorVersion = 1;\n  Int PackRevision = 1;\n}\n')
                with open(os.path.join(afi, g.XMP_FILENAME), 'w', encoding='utf-8') as f:
                    f.write(g.generate_xmp(items))

    # ── reports ──
    kit_csv = os.path.join(rep, f'kit_report_{stamp}.csv')
    pad_csv = os.path.join(rep, f'pad_report_{stamp}.csv')
    with open(kit_csv, 'w', newline='', encoding='utf-8-sig') as fk, \
            open(pad_csv, 'w', newline='', encoding='utf-8-sig') as fp_:
        wk, wp = csv.writer(fk), csv.writer(fp_)
        wk.writerow(KIT_COLS)
        wp.writerow(PAD_COLS)
        for kr, key, rel_out, _ in results:
            wk.writerow([kr.expansion, kr.kit, kr.source, kr.output, kr.status, kr.detected,
                         kr.converted, ' | '.join(kr.missing), ' | '.join(kr.unsupported),
                         ' | '.join(kr.notes)])
            for r in kr.pads:
                wp.writerow([kr.expansion, kr.kit, r.n, r.midi, r.name, r.itype, r.sample_ref,
                             r.resolved, r.validation, r.result])
            for n in range(len(kr.pads) + 1, g.MAX_PADS + 1):
                if kr.pads:
                    wp.writerow([kr.expansion, kr.kit, n, g.FIRST_MIDI_NOTE + n - 1, '', '', '', '',
                                 'n/a', P_EMPTY + ' (no record; position unverified)'])

    from collections import Counter
    c = Counter(kr.status for kr, *_ in results)
    log.info('─' * 60)
    for k, v in sorted(c.items()):
        log.info('%-28s %d', k, v)
    log.info('kit report: %s', kit_csv)
    log.info('pad report: %s', pad_csv)
    log.info('log:        %s', log_path)
    if opts.dry_run:
        log.info('DRY-RUN: no .adg, manifest or metadata files were written.')
    return 2 if any(k.startswith('ERROR') for k in c) else 0


if __name__ == '__main__':
    sys.exit(main())
