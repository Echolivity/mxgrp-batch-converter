"""
Regression tests for the 3onIt fixture.

Needs two files you own (not redistributed here):
    MXGRP_3ONIT    = path to "3onIt Kit.mxgrp"
    MXGRP_TEMPLATE = path to "Wrong Sided Kit.adg"
Samples are SYNTHETIC silent WAVs generated at the paths the kit references, so
these tests prove parsing / mapping / validation logic - not your real audio.

    python -m unittest discover -s tests -v
"""
import gzip, hashlib, os, struct, sys, tempfile, unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import mxgrp_parser as P
import mxgrp_to_drumrack as G
import batch_convert as B

KIT = os.environ.get('MXGRP_3ONIT')
TPL = os.environ.get('MXGRP_TEMPLATE')

# Ground truth supplied by the user from Maschine (pads 10-12) + names parsed from the file.
EXPECTED_PADS = [
    'Kick 3onIt 1', 'Snare 3onIt 1', 'ClosedHH 3onIt', 'OpenHH 3onIt', 'Kick 3onIt 2',
    'Snare 3onIt 2', 'Snap 3onIt', 'Shaker 3onIt', 'Tom 3onIt 2',
    'Sidestick 3onIt', 'Tom 3onIt 1', 'Vocal 3onIt',          # pads 10, 11, 12 (user-verified order)
    'Lick Guitar[100] 3onIt 1', 'Chord Guitar 3onIt', 'Lick Bass[100] 30nIt',
]


def make_wav(path, frames, extensible=False, truncate=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ch, sr, bits = 2, 44100, 24
    data = b'\0' * (frames * ch * 3)
    if extensible:
        fmt = struct.pack('<HHIIHHHHIH14s', 0xFFFE, ch, sr, sr * ch * 3, ch * 3, bits, 22, bits, 3, 1, b'\0' * 14)
    else:
        fmt = struct.pack('<HHIIHH', 1, ch, sr, sr * ch * 3, ch * 3, bits)
    body = b'WAVEfmt ' + struct.pack('<I', len(fmt)) + fmt
    body += b'LIST' + struct.pack('<I', 9) + b'INFOabcde\0'       # chunk before data, odd size
    body += b'data' + struct.pack('<I', len(data)) + data
    blob = b'RIFF' + struct.pack('<I', len(body)) + body
    open(path, 'wb').write(blob[:-truncate] if truncate else blob)


@unittest.skipUnless(KIT and TPL and os.path.isfile(KIT) and os.path.isfile(TPL),
                     'set MXGRP_3ONIT and MXGRP_TEMPLATE')
class Regression3onIt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = cls.tmp.name
        cls.lib = os.path.join(t, 'lib')
        cls.exp = os.path.join(cls.lib, 'Amplified Funk Library')
        kits = os.path.join(cls.exp, 'Groups', 'Kits')
        os.makedirs(kits)
        cls.src = os.path.join(kits, '3onIt Kit.mxgrp')
        with open(KIT, 'rb') as a, open(cls.src, 'wb') as b:
            b.write(a.read())
        cls.kp = P.parse_mxgrp(cls.src)
        for s in cls.kp.sounds:
            if s.kind == 'sampler':
                make_wav(os.path.join(cls.exp, *s.sample_rel.split('/')), 1000 + 100 * s.index,
                         extensible=(s.index % 4 == 0))
        cls.out = os.path.join(t, 'out')
        cls.rep = os.path.join(t, 'rep')
        cls.src_hash = hashlib.sha256(open(cls.src, 'rb').read()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cli(self, *extra):
        return B.main([self.lib, self.out, '--template', TPL, '--report', self.rep, *extra])

    def test_01_parser_pad_order_names_and_plugin(self):
        self.assertEqual(len(self.kp.sounds), 16)
        self.assertEqual([s.name for s in self.kp.sounds[:15]], EXPECTED_PADS)
        self.assertTrue(all(s.kind == 'sampler' for s in self.kp.sounds[:15]))
        last = self.kp.sounds[15]
        self.assertEqual((last.index, last.kind, last.plugin_name), (16, 'plugin', 'Massive'))
        self.assertEqual(self.kp.warnings, [])

    def test_02_legacy_generator_reproduces_reported_bug_new_one_fixes_it(self):
        pads = [(35 + s.index, s.name, '/x/' + s.sample_rel, 1000, s.name) for s in self.kp.sounds[:15]]

        def by_pad(xml):
            r = {}
            for b in ET.fromstring(xml.encode()).iter('DrumBranchPreset'):
                midi = 128 - int(b.find('.//ReceivingNote').get('Value'))
                r[midi - 35] = os.path.basename([e.get('Value') for e in b.iter('Path') if e.get('Value')][0])
            return r
        legacy = by_pad(G.generate_drum_rack_adg_legacy('K', [p[:4] for p in pads], TPL))
        new = by_pad(G.generate_drum_rack_adg('K', pads, TPL))
        # user's reported "initial Ableton assignment"
        self.assertEqual((legacy[10], legacy[11], legacy[12]),
                         ('Tom 3onIt 1.wav', 'Vocal 3onIt.wav', 'Sidestick 30nIt.wav'))
        # fixed: Maschine order
        self.assertEqual((new[10], new[11], new[12]),
                         ('Sidestick 30nIt.wav', 'Tom 3onIt 1.wav', 'Vocal 3onIt.wav'))
        for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 13, 14, 15):
            self.assertEqual(legacy[n], new[n], f'pad {n} differs from upstream')
        self.assertNotIn(16, new)

    def test_02b_special_characters_are_xml_escaped(self):
        pads = [(36, 'Kick & "Bass" <1>', 'H:/lib/Samples/Kick & "Bass" <1>.wav', 1000, 'Kick & "Bass" <1>')]
        xml = G.generate_drum_rack_adg('Exp & Co - Kit <x>', pads, TPL)
        self.assertEqual(G.validate_adg_xml(xml, [(36, pads[0][2])]), [])
        ET.fromstring(xml.encode())

    def test_03_dry_run_writes_nothing(self):
        self.assertEqual(self.run_cli('--dry-run'), 0)
        self.assertFalse(os.path.exists(self.out))

    def test_04_execute_and_verify_adg(self):
        self.assertEqual(self.run_cli('--execute'), 0)
        adg = os.path.join(self.out, 'Amplified Funk Library', '3onIt Kit.adg')
        xml = gzip.open(adg).read().decode()
        root = ET.fromstring(xml.encode())
        got = {}
        for b in root.iter('DrumBranchPreset'):
            midi = 128 - int(b.find('.//ReceivingNote').get('Value'))
            got[midi] = (b.find('.//UserName').get('Value'),
                         os.path.basename([e.get('Value') for e in b.iter('Path') if e.get('Value')][0]))
        self.assertEqual(sorted(got), list(range(36, 51)))                     # notes 36-50 only
        self.assertEqual([got[36 + i][0] for i in range(15)], EXPECTED_PADS)   # pad names in Maschine order
        for i, s in enumerate(self.kp.sounds[:15]):
            self.assertEqual(got[36 + i][1], os.path.basename(s.sample_rel))
        self.assertEqual(len(list(root.iter('DrumBranchPreset'))), 15)
        self.assertNotIn('Wrong Sided', xml)
        self.assertEqual(hashlib.sha256(open(self.src, 'rb').read()).hexdigest(), self.src_hash)

    def test_05_pad_csv_reports_massive_as_unsupported_plugin(self):
        import csv, glob
        f = sorted(glob.glob(os.path.join(self.rep, 'pad_report_*.csv')))[-1]
        rows = list(csv.DictReader(open(f, encoding='utf-8-sig')))
        r16 = [r for r in rows if r['Maschine pad number'] == '16' and r['Kit'] == '3onIt Kit'][-1]
        self.assertEqual(r16['Conversion result'], 'UNSUPPORTED_PLUGIN')
        self.assertIn('Massive', r16['Instrument type'])
        self.assertEqual(r16['MIDI note'], '51')

    def test_06_second_run_skips_and_never_overwrites(self):
        adg = os.path.join(self.out, 'Amplified Funk Library', '3onIt Kit.adg')
        before = os.stat(adg).st_mtime_ns
        self.assertEqual(self.run_cli('--execute'), 0)
        self.assertEqual(os.stat(adg).st_mtime_ns, before)

    def test_07_missing_and_corrupt_samples_block_the_kit(self):
        s = self.kp.sounds[2]
        wav = os.path.join(self.exp, *s.sample_rel.split('/'))
        os.rename(wav, wav + '.bak')
        try:
            kr = B.analyse_kit(self.src, 'E', self.exp, 'K', '/o.adg', _Opts())
            self.assertEqual(kr.status, B.MISSING)
            self.assertTrue(any('ClosedHH' in m for m in kr.missing))
        finally:
            os.rename(wav + '.bak', wav)
        make_wav(wav, 5000, truncate=2000)
        try:
            kr = B.analyse_kit(self.src, 'E', self.exp, 'K', '/o.adg', _Opts())
            self.assertEqual(kr.status, B.MISSING)
            self.assertIn('truncated', ' '.join(kr.missing))
        finally:
            make_wav(wav, 1000 + 100 * s.index)

    def test_08_wav_reader_extensible_and_list_chunk(self):
        p = os.path.join(self.tmp.name, 'x.wav')
        make_wav(p, 777, extensible=True)
        info = G.read_wav_info(p)
        self.assertEqual((info['frames'], info['bits'], info['channels']), (777, 24, 2))


class _Opts:
    template = TPL
    accept_parse_warnings = False
    accept_short_kits = False
    allow_missing_samples = False


if __name__ == '__main__':
    unittest.main()
