#!/usr/bin/env python3
"""
Structured .mxgrp reader (replaces upstream's ".wav substring" scan).

What is known about the format (reverse-engineered from "3onIt Kit.mxgrp";
see FORMAT_NOTES.md - only ONE real kit has been inspected so far):

  * NI container header ("hsin"/"DSIN" chunks) followed by a Boost-style
    binary archive ("serialization::archive").
  * Text is plain ASCII (NOT UTF-16LE as the upstream README says), stored as
        0x01  <len:1 byte>  <len bytes of text>
  * Each Sound is serialised in order. Per sound the stream contains:
        sound name  ->  [class names, first use only]  ->  instrument payload
    Sampler payload : "Samples/....wav", then an absolute authoring path
                      ("/Volumes/..."), then the library name.
    Plugin payload  : "NI::MASCHINE::DATA::PluginHost", then a plugin id
                      such as "Native Instruments:VST:INSTR:1315523937",
                      then the plugin's display name.
  * A multi-zone sampler repeats the sample record with NO new sound name
    (Lick Bass[100] repeats 12x in the fixture).
  * Sounds appear in pad order in the fixture (pad 16 = Massive, last).

NOT yet known (no fixture): how an *empty* pad between two filled pads is
stored. The converter therefore refuses to guess for kits with < 16 sounds
unless the user opts in (see batch_convert.py --accept-short-kits).
"""

import re
from dataclasses import dataclass, field

PARSER_VERSION = "3.1.0"

AUDIO_EXTS = ('.wav', '.aif', '.aiff', '.flac', '.ogg', '.mp3', '.rx2', '.rex', '.wv')
SUPPORTED_EXTS = ('.wav',)

# Classes that live inside a sound but are not the instrument.
KNOWN_NON_INSTRUMENT_CLASSES = {
    'Saturator', 'NoteEvent', 'Gate', 'PlateReverb', 'Phaser', 'BeatDelay',
    'Filter', 'Maximizer', 'Compressor', 'EQ', 'Reverb', 'Delay', 'Limiter',
    'Distortion', 'Lofi', 'BitCrusher', 'Chorus', 'Flanger', 'Tremolo',
    'Transient', 'Ice', 'Metaverb', 'Choral', 'Ring', 'Resochord',
    'Inflator',
    # seen in the user's library dry-run (effects / modulation / event records, not instruments)
    'TransientDesigner', 'GrainDelay', 'GrainStretch', 'PerformanceFX', 'FloatModulationEvent',
    'Iceverb', 'LoFi', 'MidiEvent', 'FreqShifter', 'Reflex', 'CabinetEmulation',
}
# Deliberately NOT whitelisted until a real kit is inspected: AudioModule, AudioEvent
# (could be an audio-based sound type that has no sampler/plugin record).
INSTRUMENT_CLASSES = {'Sampler': 'sampler', 'PluginHost': 'plugin'}
# Maschine built-in drum synth engines (seen as NI::MASCHINE::DATA::<name> in synth/hybrid kits)
SYNTH_CLASSES = {'Kick', 'Snare', 'Hihat', 'Perc', 'Tom', 'Cymbal', 'FM', 'Clap'}


def is_synth_class(c):
    return c in SYNTH_CLASSES or c.endswith('Synth')    # BassSynth, PolySynth, ...
MIN_NAME_LEN = 3
MIN_SOUND_SPACING = 2300     # non-empty sound records are >= ~2300 bytes apart; closer strings are suspect
HARD_SPACING = 1200          # closer than this is always noise
# module / parameter labels that appear as stand-alone strings inside pad records
_FX_LABELS = {x.lower() for x in (
    'Reverb', 'Chorus', 'Phaser', 'Flanger', 'Filter', 'Envelope', 'FM', 'Beat Delay', 'Beat Del',
    'Grain Delay', 'Grain Del', 'Freq Shft', 'Freq Shifter', 'Lofi', 'Voice', 'Ice', 'Compressor',
    'EQ', 'Saturator', 'Gate', 'Maximizer', 'Limiter', 'Distortion', 'Metaverb', 'Resochord',
    'Macro', 'Macros', 'Mod', 'Delay', 'Tremolo', 'Cabinet', 'Transient', 'Filter Mode',
    'FM Amount', 'Freq Coarse', 'Freq Fine', 'Division', 'Frequency', 'Reflex', 'Decay', 'Release',
    'Attack', 'Sustain', 'Cutoff', 'Resonance', 'Dist Drive', 'Size', 'Feedback', 'Mix', 'Amount',
    'Drive', 'Gain', 'Level', 'Time', 'Dry', 'Wet', 'Tone', 'Pitch', 'Tune', 'Color', 'Width',
    'Rate', 'Depth', 'Speed', 'Shape', 'Global', 'Panning')}
_LABEL_WORDS = {'lfo', 'osc', 'knob', 'macro', 'macros', 'env', 'envelope', 'freq', 'cutoff',
                'resonance', 'reflex', 'frequency', 'filter', 'mod', 'dist'}
_SCENE_RE = re.compile(r'^(\d+-.+|Pattern \d+)$')
_DEFAULT_NAME_RE = re.compile(r'^Sound \d+$')

_PLUGIN_ID_RE = re.compile(r'^[^:]+:(VST3?|AU|AAX):[A-Z]*:?\d*', re.I)
_ABS_RE = re.compile(r'^(/|[A-Za-z]:[\\/]|\\\\)')
_CLASS_RE = re.compile(rb'NI::MASCHINE::DATA::([A-Za-z0-9_]+)')

NAME_WINDOW = 600  # max distance (bytes) from a name to its instrument anchor


@dataclass
class Token:
    pos: int
    end: int
    kind: str          # path | abs | class | plugid | str
    text: str


@dataclass
class Sound:
    index: int                      # 1-based serialised position == pad number
    name: str
    kind: str                       # sampler | plugin | synth | empty | unknown
    sample_rel: str = ''
    zone_paths: list = field(default_factory=list)   # extra DIFFERENT sample refs
    classes: list = field(default_factory=list)
    plugin_id: str = ''
    plugin_name: str = ''
    offset: int = 0
    warnings: list = field(default_factory=list)


@dataclass
class KitParse:
    sounds: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    unknown_classes: list = field(default_factory=list)
    info: list = field(default_factory=list)      # non-blocking notes
    raw_audio_hits: int = 0
    structured_audio_tokens: int = 0
    library_names: list = field(default_factory=list)


def _printable(b):
    return 32 <= b < 127


def tokenize(data):
    """Find every `01 <len> <ascii>` string and every NI class name."""
    toks = []
    n = len(data)
    i = 1
    while i < n - 2:
        L = data[i]
        if data[i - 1] == 1 and 2 <= L <= 220 and i + 1 + L <= n:
            raw = data[i + 1:i + 1 + L]
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                text = None
            if text is not None and all(c.isprintable() or c == '\xa0' for c in text) \
                    and any(c.isalnum() for c in text):
                low = text.lower()
                if ((low.endswith(AUDIO_EXTS) and ('/' in text or '\\' in text))
                        or (low.startswith('samples/') and re.search(r'\.[a-z0-9]{2,5}$', low))):
                    kind = 'path'            # a bare 'Name.wav' is a pad NAME, not a sample path
                elif _ABS_RE.match(text):
                    kind = 'abs'
                elif _PLUGIN_ID_RE.match(text) and text.count(':') >= 2:
                    kind = 'plugid'
                else:
                    kind = 'str'
                toks.append(Token(i - 1, i + 1 + L, kind, text))
                i += 1 + L
                continue
        i += 1
    for m in _CLASS_RE.finditer(data):
        toks.append(Token(m.start(), m.end(), 'class', m.group(1).decode()))
    toks.sort(key=lambda t: t.pos)
    # drop generic strings that overlap a class token
    out = []
    for t in toks:
        if out and t.kind != 'class' and t.pos < out[-1].end:
            continue
        if out and t.kind == 'class' and out[-1].kind != 'class' and t.pos < out[-1].end:
            out.pop()
        out.append(t)
    return out


def parse_mxgrp_bytes(data, stem=None):
    """Name-sequence parser (v3).

    Layout observed in every kit inspected so far (3onIt + 37 others):
        ... "serialization::archive", <library>, <library>, <id token>,
        <sound name 1> ... <sound name 16>, <group name == file stem>, <scenes...>
    Each pad's content is whatever lies between its name and the next name.
    """
    kp = KitParse()
    kp.raw_audio_hits = len(re.findall(rb'(?i)\.(wav|aif|aiff|flac|ogg|mp3)', data))
    toks = tokenize(data)
    strs = [t for t in toks if t.kind == 'str']

    start = next((i for i, t in enumerate(strs) if t.text == 'serialization::archive'), None)
    if start is None:
        kp.errors.append('header not recognised (no archive marker)')
        return kp
    marker_pos = strs[start].pos
    # header strings (library name(s), id token) sit within ~300 bytes of the marker;
    # newer kits have none. The first sound name is always much further away.
    header = [t for t in strs[start + 1:] if t.pos - marker_pos < 300]
    libnames = {t.text for t in header}
    for k, t in enumerate(toks):
        if t.kind == 'abs' and k + 1 < len(toks) and toks[k + 1].kind == 'str' \
                and toks[k + 1].pos - t.end < 8:
            libnames.add(toks[k + 1].text)
    kp.library_names = sorted(libnames)
    id_token = header[-1].text if header else None
    body = [t for t in strs[start + 1:] if t.pos - marker_pos >= 300]

    # end of the sound list = group name
    g = None
    if stem:
        hits = [i for i, t in enumerate(body) if t.text.lower() == stem.lower()]
        g = hits[-1] if hits else None            # last: a pad may share the group's name
    if g is None:
        sc = next((i for i, t in enumerate(body) if _SCENE_RE.match(t.text)), None)
        if sc:
            g = sc - 1
            kp.info.append(f'group name differs from file name; assumed "{body[g].text}"')
    if g is None:
        kp.errors.append('end of sound list (group name) not found')
        return kp
    end_pos = body[g].pos

    # a macro / chain label "A / B / C" is stored again as separate strings A, B, C
    label_parts = set()
    for t in strs:
        if ' / ' in t.text:
            label_parts.update(x.strip().lower() for x in t.text.split(' / '))
    raw = [t for t in body[:g]
           if not (t.text in libnames or t.text == id_token or len(t.text) < MIN_NAME_LEN
                   or ':' in t.text or ' / ' in t.text or len(t.text) > 64
                   or t.text.lower() in _FX_LABELS)]

    def _has_content(lo, hi):
        for t in toks:
            if lo <= t.pos < hi:
                if t.kind in ('path', 'plugid'):
                    return True
                if t.kind == 'class' and t.text in INSTRUMENT_CLASSES:
                    return True
        return False

    # A string that is also a component of an "A / B / C" chain label is parameter text,
    # UNLESS it has sample / plugin content of its own (chain labels can quote a sample name).
    raw = [t for i, t in enumerate(raw)
           if not (t.text.strip().lower() in label_parts
                   and not _has_content(t.pos, raw[i + 1].pos if i + 1 < len(raw) else end_pos))]

    def evid(i):
        lo = raw[i].pos
        hi = raw[i + 1].pos if i + 1 < len(raw) else end_pos
        for t in toks:
            if lo <= t.pos < hi:
                if t.kind in ('path', 'plugid'):
                    return True
                if t.kind == 'class' and (t.text in INSTRUMENT_CLASSES or is_synth_class(t.text)):
                    return True
        return False

    names, kept_idx = [], []
    for i, t in enumerate(raw):
        if names:
            prev = names[-1]
            close = t.pos - prev.pos < MIN_SOUND_SPACING
            if close and not _DEFAULT_NAME_RE.match(t.text) and not _DEFAULT_NAME_RE.match(prev.text):
                if t.pos - prev.pos < HARD_SPACING:
                    continue                               # always noise
                # 1200-2300 bytes apart: keep both (a wrong count is flagged later)
        names.append(t)
        kept_idx.append(i)

    # Strings inside a plugin's saved state (Massive "Macro", "Osc2", ...) look like names.
    # After a plugin pad, a candidate only counts as a new pad if its content shows an
    # instrument (sample / plugin / engine class) or it is a default "Sound N" name.
    def has_evidence(lo, hi):
        for t in toks:
            if lo <= t.pos < hi:
                if t.kind in ('path', 'plugid'):
                    return True
                if t.kind == 'class' and t.text in INSTRUMENT_CLASSES:   # synth classes can occur inside plugin state
                    return True
        return False
    accepted, after_plugin = [], False
    for i, nt in enumerate(names):
        hi = names[i + 1].pos if i + 1 < len(names) else end_pos
        ev = has_evidence(nt.pos, hi)
        if after_plugin and not ev and not _DEFAULT_NAME_RE.match(nt.text):
            continue
        accepted.append(nt)
        is_plug = any((t.kind == 'plugid' or (t.kind == 'class' and t.text == 'PluginHost'))
                      for t in toks if nt.pos <= t.pos < hi)
        after_plugin = is_plug
    names = accepted

    first_pos = names[0].pos if names else end_pos
    for t in toks:
        if t.kind == 'path' and t.pos < first_pos:
            kp.warnings.append(f'sample reference at byte {t.pos} before first sound name')

    seen_classes = []
    for i, nt in enumerate(names):
        lo = nt.pos
        hi = names[i + 1].pos if i + 1 < len(names) else end_pos
        span = [t for t in toks if lo <= t.pos < hi]
        snd = Sound(index=i + 1, name=nt.text, kind='unknown', offset=lo)
        paths = [t.text for t in span if t.kind == 'path']
        plug = [t.text for t in span if t.kind == 'plugid']
        classes = [t.text for t in span if t.kind == 'class']
        snd.classes = classes
        for c in classes:
            if c not in seen_classes:
                seen_classes.append(c)
        if plug or 'PluginHost' in classes:
            snd.kind = 'plugin'
            snd.plugin_id = plug[0] if plug else ''
            if plug:
                after = [t for t in span if t.kind == 'str' and t.pos > next(x.pos for x in span if x.kind == 'plugid')]
                if after and after[0].pos - next(x.pos for x in span if x.kind == 'plugid') < 400:
                    snd.plugin_name = after[0].text
            if paths:
                snd.warnings.append('sample reference inside plugin pad ignored: ' + paths[0])
        elif paths:
            snd.kind = 'sampler'
            snd.sample_rel = paths[0]
            snd.zone_paths = sorted(set(paths) - {paths[0]})
            kp.structured_audio_tokens += len(paths)
        elif 'Sampler' in classes:
            snd.kind = 'sampler'                      # sampler with no sample loaded
        elif any(is_synth_class(c) for c in classes):
            snd.kind = 'synth'
        elif _DEFAULT_NAME_RE.match(nt.text):
            snd.kind = 'empty'
        kp.sounds.append(snd)
    kp.structured_audio_tokens = sum(1 for t in toks if t.kind == 'path')

    # More than 16 candidates: strings with no content (label-like) cannot be pads if
    # there are already exactly 16 pads with real content (a 17th real pad is impossible).
    if len(kp.sounds) > 16:
        extras = [x for x in kp.sounds if x.kind == 'unknown']
        if len(kp.sounds) - len(extras) == 16:
            kp.info.append('ignored label-like strings: ' + ', '.join(repr(x.name) for x in extras))
            kp.sounds = [x for x in kp.sounds if x.kind != 'unknown']
            for n, x in enumerate(kp.sounds, 1):
                x.index = n

    kp.unknown_classes = [c for c in seen_classes
                          if c not in KNOWN_NON_INSTRUMENT_CLASSES
                          and c not in INSTRUMENT_CLASSES and not is_synth_class(c)]
    if kp.unknown_classes:
        kp.info.append('other NI classes (ignored): ' + ', '.join(kp.unknown_classes))

    if kp.structured_audio_tokens != kp.raw_audio_hits:
        covered = [(t.pos, t.end) for t in toks if t.kind == 'path']
        for m in re.finditer(rb'(?i)\.(wav|aif|aiff|flac|ogg|mp3)', data):
            if not any(a <= m.start() < b for a, b in covered):
                ctx = data[max(0, m.start() - 80):m.end() + 4]
                txt = ''.join(chr(c) if 32 <= c < 127 else '.' for c in ctx)
                if re.search(r'(?i)samples[/\\]', txt) and not re.search(r'(?i)file:|://|%20|%2f|[A-Za-z]:/Users', txt):
                    kp.warnings.append(f'unparsed sample reference @{m.start()}: {txt}')
                else:
                    kp.info.append(f'non-library audio URL inside plugin state ignored @{m.start()}')
    return kp


def parse_mxgrp(filepath):
    import os
    with open(filepath, 'rb') as f:
        data = f.read()
    return parse_mxgrp_bytes(data, os.path.splitext(os.path.basename(filepath))[0])


if __name__ == '__main__':
    import sys
    kp = parse_mxgrp(sys.argv[1])
    for s in kp.sounds:
        print(s.index, s.kind, repr(s.name), s.sample_rel or s.plugin_id or ','.join(s.classes[:3]),
              f'(+{len(s.zone_paths)} zones)' if s.zone_paths else '', s.plugin_name)
    for w in kp.errors + kp.warnings:
        print('WARN', w)
