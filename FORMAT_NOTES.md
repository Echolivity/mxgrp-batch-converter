# .mxgrp format notes (reverse-engineered)

Based on **one** real kit (`Amplified Funk Library\Groups\Kits\3onIt Kit.mxgrp`, 83,632 bytes). Everything
below is an observation from that file, not a specification. Treat other kits as unverified until the dry-run
report says otherwise.

* Container: NI chunk header (`hsin` / `DSIN` tags) then a Boost-style binary archive (`serialization::archive`).
* **Text is ASCII, not UTF-16LE** (upstream README is wrong on this; upstream only worked because it dropped NUL bytes).
* Strings are `0x01 <len:1 byte> <text>`.
* 16 sound records, serialised in pad order. Each: name -> (class names, first use only) -> instrument payload.
  * Sampler: `Samples/<rel>.wav`, then absolute authoring path (`/Volumes/FantomHD/...`), then library name.
  * Plugin (pad 16): `NI::MASCHINE::DATA::PluginHost`, `Native Instruments:VST:INSTR:1315523937`, display name `Massive`.
* Multi-zone sampler (`Lick Bass[100]`, pad 15) repeats the sample record 12x with no new sound name.
* Pad names are NOT the wav filename (`Sidestick 3onIt` -> `Sidestick 30nIt.wav`; the file itself contains the `30nIt` typo).
* Unknown: how an empty pad *between* filled pads is stored; whether pads can be re-noted (default 36+index assumed);
  whether other instrument classes (drum synths, audio) exist and how they serialise.

## Ableton side
`ReceivingNote = 128 - MIDI note`. The template lists its 16 branches in XML order
`92 91 90 89 88 87 86 85 84 81 83 82 80 79 78 77`, so XML positions 10-12 are notes 47, 45, 46 (not 45, 46, 47).
Upstream mapped pad i to XML position i -> pads 10-12 came out rotated. The fix selects branches by note.

## Update: 38 kits inspected (3onIt + 37 samples from the user's library)
* **Every kit has exactly 16 pad records**, in pad order. Unused pads are stored as default-named records ("Sound 2" ... "Sound 16"); they are NOT omitted.
* Layout: `serialization::archive`, library name x2, an id token (e.g. `594`, `X57`), then 16 pad names, then the group name (= file name), then scenes ("1-100 Bpm", "Pattern 1").
* A pad's content is whatever lies between its name and the next name:
  * sample: `Samples/...wav` (+ library path); multi-zone samplers repeat the record
  * plugin: `Native Instruments:VST:INSTR:<id>` (Massive, Monark seen); the plugin's saved state contains parameter-name strings ("Macro", "Osc2", "Env1") which must not be mistaken for pads
  * Maschine drum synth: `NI::MASCHINE::DATA::Kick/Snare/Hihat/Perc/Tom/Cymbal/FM/BassSynth/PolySynth` (class name is written only at first use)
* 2-character strings inside binary data look like names; pad names are required to be >= 3 characters and >= 1800 bytes apart.
* Old parser (v2) found pads only by sample/plugin anchors, so drum-synth pads were invisible and later pads shifted. v3 reads the name sequence.
* Still unverified: pad order against Maschine for any kit other than 3onIt; custom pad->note maps; kits with a pad name under 3 characters.
