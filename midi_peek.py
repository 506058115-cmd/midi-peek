#!/usr/bin/env python3
"""Summarize Standard MIDI File structure and note-event counts."""

import argparse
from pathlib import Path
import stat
import sys


MAX_BYTES = 128 * 1024 * 1024
MAX_EVENTS = 2_000_000


class MidiError(ValueError):
    pass


def read_exact(stream, size, limit, label):
    if size < 0 or size > limit - stream.tell():
        raise MidiError(f"truncated {label}")
    data = stream.read(size)
    if len(data) != size:
        raise MidiError(f"truncated {label}")
    return data


def skip_exact(stream, size, limit, label):
    if size < 0 or size > limit - stream.tell():
        raise MidiError(f"truncated {label}")
    stream.seek(size, 1)


def read_vlq(stream, limit, label):
    value = 0
    for _ in range(4):
        byte = read_exact(stream, 1, limit, label)[0]
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value
    raise MidiError(f"invalid {label}")


def track_events(stream, track_end, track_number, totals, tempo_events, event_count):
    tick = 0
    sequence = 0
    running_status = None
    while stream.tell() < track_end:
        if event_count >= MAX_EVENTS:
            raise MidiError("event limit exceeded")
        tick += read_vlq(stream, track_end, "delta time")
        first = read_exact(stream, 1, track_end, "event status or data byte")[0]
        first_data = None
        if first < 0x80:
            if running_status is None:
                raise MidiError("data byte without running status")
            status = running_status
            first_data = first
        else:
            status = first

        sequence += 1
        event_count += 1
        if 0x80 <= status <= 0xEF:
            running_status = status
            command = status >> 4
            length = 1 if command in (0xC, 0xD) else 2
            data = [] if first_data is None else [first_data]
            while len(data) < length:
                value = read_exact(stream, 1, track_end, "channel event data")[0]
                if value & 0x80:
                    raise MidiError("invalid channel event data")
                data.append(value)
            totals["channel_events"] += 1
            totals["channels"].add(status & 0x0F)
            if command == 0x9 and data[1] != 0:
                totals["note_on"] += 1
            elif command == 0x8 or (command == 0x9 and data[1] == 0):
                totals["note_off"] += 1
            elif command == 0xC:
                totals["program_changes"] += 1
            continue

        running_status = None
        if status == 0xFF:
            meta_type = read_exact(stream, 1, track_end, "meta event type")[0]
            length = read_vlq(stream, track_end, "meta event length")
            if length > track_end - stream.tell():
                raise MidiError("meta event exceeds its track")
            if meta_type == 0x2F:
                if length or stream.tell() != track_end:
                    raise MidiError("end-of-track event must be empty and last")
                totals["meta_events"] += 1
                return tick, event_count
            if meta_type == 0x51:
                if length != 3:
                    raise MidiError("tempo event must contain three bytes")
                tempo = int.from_bytes(read_exact(stream, 3, track_end, "tempo event"), "big")
                if not tempo:
                    raise MidiError("tempo must be nonzero")
                tempo_events.append((tick, track_number, sequence, tempo))
                totals["tempo_events"] += 1
            else:
                skip_exact(stream, length, track_end, "meta event")
            totals["meta_events"] += 1
            continue

        if status in (0xF0, 0xF7):
            length = read_vlq(stream, track_end, "system-exclusive length")
            skip_exact(stream, length, track_end, "system-exclusive event")
            totals["sysex_events"] += 1
            continue
        raise MidiError("unsupported event status")

    raise MidiError("track is missing its end-of-track event")


def inspect(path):
    source = Path(path)
    file_stat = source.stat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise MidiError("input must be a regular file")
    if file_stat.st_size > MAX_BYTES:
        raise MidiError("input exceeds the 128 MiB limit")

    totals = {"channel_events": 0, "note_on": 0, "note_off": 0, "program_changes": 0,
              "meta_events": 0, "tempo_events": 0, "sysex_events": 0, "channels": set()}
    tempo_events = []
    durations = []
    event_count = 0
    with source.open("rb") as stream:
        if read_exact(stream, 4, file_stat.st_size, "header chunk") != b"MThd":
            raise MidiError("missing MThd header")
        header_length = int.from_bytes(read_exact(stream, 4, file_stat.st_size, "header length"), "big")
        if header_length < 6 or header_length > file_stat.st_size - stream.tell():
            raise MidiError("invalid header length")
        header = read_exact(stream, 6, file_stat.st_size, "header data")
        file_format = int.from_bytes(header[0:2], "big")
        track_count = int.from_bytes(header[2:4], "big")
        division = int.from_bytes(header[4:6], "big")
        skip_exact(stream, header_length - 6, file_stat.st_size, "extended header")
        if file_format not in (0, 1, 2) or not track_count or (file_format == 0 and track_count != 1):
            raise MidiError("unsupported format or track count")

        if division & 0x8000:
            fps_code = ((division >> 8) & 0xFF) - 256
            ticks_per_frame = division & 0xFF
            frame_rates = {-24: 24.0, -25: 25.0, -29: 30000 / 1001, -30: 30.0}
            if fps_code not in frame_rates or not ticks_per_frame:
                raise MidiError("invalid SMPTE timing division")
            timing = ("SMPTE", fps_code, ticks_per_frame, frame_rates[fps_code])
        else:
            if not division:
                raise MidiError("ticks per quarter note must be nonzero")
            timing = ("PPQN", division)

        for track_number in range(track_count):
            if read_exact(stream, 4, file_stat.st_size, "track chunk") != b"MTrk":
                raise MidiError("missing MTrk chunk")
            track_length = int.from_bytes(read_exact(stream, 4, file_stat.st_size, "track length"), "big")
            track_end = stream.tell() + track_length
            if track_end > file_stat.st_size:
                raise MidiError("track extends past end of file")
            end_tick, event_count = track_events(
                stream, track_end, track_number, totals, tempo_events, event_count
            )
            durations.append(end_tick)
        trailing = file_stat.st_size - stream.tell()

    max_tick = max(durations, default=0)
    seconds = None
    if file_format != 2:
        if timing[0] == "SMPTE":
            seconds = max_tick / (timing[3] * timing[2])
        else:
            ppqn = timing[1]
            tempo = 500_000
            previous_tick = 0
            seconds = 0.0
            for tick, _, _, new_tempo in sorted(tempo_events):
                if tick > max_tick:
                    break
                seconds += (tick - previous_tick) * tempo / (ppqn * 1_000_000)
                previous_tick = tick
                tempo = new_tempo
            seconds += (max_tick - previous_tick) * tempo / (ppqn * 1_000_000)

    return file_format, track_count, timing, event_count, totals, max_tick, seconds, trailing


def format_duration(seconds):
    millis = round(seconds * 1000)
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, fraction = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}.{fraction:03}"


def main():
    parser = argparse.ArgumentParser(
        description="Count Standard MIDI File events without displaying track names or note data."
    )
    parser.add_argument("input", help="Standard MIDI File (.mid)")
    args = parser.parse_args()
    try:
        file_format, tracks, timing, events, totals, ticks, seconds, trailing = inspect(args.input)
    except MidiError as error:
        print(f"midi-peek: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"midi-peek: {error.strerror or 'unable to read input'}", file=sys.stderr)
        return 2

    print(f"Format: {file_format} ({('single track', 'synchronous tracks', 'independent tracks')[file_format]})")
    print(f"Tracks: {tracks}")
    if timing[0] == "PPQN":
        print(f"Timing: {timing[1]} ticks per quarter note")
    else:
        print(f"Timing: SMPTE {timing[1]} fps, {timing[2]} ticks per frame")
    print(f"Events: {events} total; {totals['channel_events']} channel, {totals['meta_events']} meta, {totals['sysex_events']} SysEx")
    print(f"Notes: {totals['note_on']} on, {totals['note_off']} off")
    print(f"Channels used: {len(totals['channels'])}")
    print(f"Program changes: {totals['program_changes']}")
    print(f"Tempo events: {totals['tempo_events']}")
    print(f"Longest track position: {ticks} ticks")
    if seconds is None:
        print("Duration: not available for independent-track format")
    else:
        suffix = " (default 500,000 µs/quarter when no tempo event is present)" if timing[0] == "PPQN" and not totals["tempo_events"] else ""
        print(f"Duration estimate: {format_duration(seconds)}{suffix}")
    print(f"Bytes after declared tracks: {trailing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
