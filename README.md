# midi-peek

Summarize Standard MIDI File tracks, timing, channel activity, note-event counts, and tempo events without printing track names or note data.

## Use

Requires Python 3.10+; no packages to install.

```sh
python midi_peek.py song.mid
```

The parser accepts SMF formats 0, 1, and 2, including PPQN and SMPTE timing divisions. Format 2 contains independent tracks, so it has no single duration estimate. PPQN duration uses the tempo events in the file, or the MIDI default of 500,000 microseconds per quarter note when none are present. Simultaneous tempo events in separate tracks are resolved in track order for the estimate.

Input must be a regular file no larger than 128 MiB. The tool counts container events but does not render audio, expose track names or note values, or validate instrument playback. See the [MIDI Association Standard MIDI Files specification](https://midi.org/standard-midi-files-specification).

## License

MIT
