from __future__ import annotations

import argparse
import signal
import sys
import time
import wave
from datetime import datetime, timezone

from meetingsai_recorder.capture import ChunkCapturer
from meetingsai_recorder.config import ConfigError, load_settings
from meetingsai_recorder.device import find_input_device
from meetingsai_recorder.logging_setup import setup_logging
from meetingsai_recorder.outbox import Outbox, Segment, new_segment_id
from meetingsai_recorder.provider import OpenAITranscriptionProvider, ProviderError
from meetingsai_recorder.sync import VpsClient, SyncError


log = None
_shutdown = False


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _handle_sigint(signum, frame):
    global _shutdown
    _shutdown = True


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="meetingsai-recorder")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--duration", type=int, default=0,
                   help="seconds; 0 = until Ctrl-C or silence-timeout")
    p.add_argument("--silence-timeout", type=int, default=0,
                   help="seconds; 0 = disabled.  If set, recorder exits when "
                        "the newest chunk is silent and the last non-silent "
                        "chunk was more than this many seconds ago.")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--save-audio", type=str, default=None)
    p.add_argument("--silence-threshold", type=int, default=None)
    p.add_argument("--meeting-id", type=str, default=None)
    p.add_argument("--new-meeting", type=str, default=None)
    p.add_argument("--pick-meeting", action="store_true")
    return p.parse_args(argv)


def _save_wav(path, audio, samplerate):
    try:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(samplerate)
            wf.writeframes(audio.tobytes())
        log.info("saved diagnostic wav -> %s", path)
    except Exception as e:
        log.warning("could not save diagnostic wav: %s", e)


def _drain_outbox(outbox, vps, meeting_id, batch_size):
    while True:
        batch = outbox.lease_batch(limit=batch_size)
        if not batch:
            return
        payload = [{"segment_id": s.segment_id, "text": s.text,
                    "started_at": s.started_at, "ended_at": s.ended_at,
                    "model": s.model} for s in batch]
        try:
            res = vps.push_batch(meeting_id, payload)
            outbox.ack([s.segment_id for s in batch])
            stats = outbox.stats()
            log.info("pushed accepted=%d duplicates=%d outbox_pending=%d",
                     res.accepted, res.duplicates, stats.get("pending", -1))
        except Exception as e:
            outbox.nack([s.segment_id for s in batch], str(e))
            log.warning("push failed: %s", e)
            return


def main(argv=None) -> int:
    global log
    args = parse_args(argv if argv is not None else sys.argv[1:])
    setup_logging("DEBUG" if args.verbose else "INFO")
    import logging
    log = logging.getLogger("meetingsai.main")

    try:
        cfg = load_settings()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.silence_threshold is not None:
        object.__setattr__(cfg, "silence_threshold", args.silence_threshold)

    needs_vps = (not args.dry_run) or bool(args.new_meeting) or bool(args.pick_meeting)
    vps = None
    if needs_vps:
        vps = VpsClient(cfg.base_url, cfg.password)
        try:
            vps.login()
        except Exception as e:
            print(f"login failed: {e}", file=sys.stderr)
            return 3

    meeting_id = None
    if args.meeting_id:
        meeting_id = args.meeting_id
    elif args.new_meeting:
        m = vps.create_meeting(title=args.new_meeting)
        meeting_id = m["id"]
        print(f"[new meeting] id={meeting_id} title={m['title']!r}")
    elif args.pick_meeting:
        items = vps.list_meetings(limit=20)
        if not items:
            print("[pick] no meetings found"); return 2
        for i, mm in enumerate(items, 1):
            print(f"  [{i:2d}] {mm['title']}   ({mm['status']})  id={mm['id']}")
        raw = input("Pick a meeting by number, or Enter to cancel: ").strip()
        if not raw: return 2
        idx = int(raw)
        if idx < 1 or idx > len(items): return 2
        meeting_id = items[idx - 1]["id"]
    elif cfg.meeting_id_is_placeholder:
        print("no meeting id: use --new-meeting, --pick-meeting, --meeting-id, "
              "or set MEETINGSAI_MEETING_ID", file=sys.stderr)
        if vps: vps.close()
        return 2
    else:
        meeting_id = cfg.meeting_id

    device_idx = find_input_device(cfg.vb_cable_device_name)
    capturer = ChunkCapturer(device_idx, cfg.samplerate, cfg.chunk_seconds)
    outbox = Outbox()
    provider = OpenAITranscriptionProvider(api_key=cfg.openai_api_key,
                                           model=cfg.transcription_model)

    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        signal.signal(signal.SIGTERM, _handle_sigint)
    except (ValueError, AttributeError):
        pass

    start = time.time()
    chunks = 0
    last_speech = time.time()

    log.info("meeting_id=%s model=%s dry_run=%s samplerate=%d silence_timeout=%d",
             meeting_id, cfg.transcription_model, args.dry_run,
             cfg.samplerate, args.silence_timeout)

    while not _shutdown:
        if args.duration and (time.time() - start) >= args.duration:
            log.info("duration reached"); break

        audio = capturer.read_chunk()
        chunks += 1
        peak = capturer.peak(audio)
        if args.save_audio:
            _save_wav(args.save_audio, audio, cfg.samplerate)

        silent = capturer.is_silent(audio, cfg.silence_threshold)
        if silent:
            log.info("chunk %d peak=%d silent -> skipped", chunks, peak)
            if args.silence_timeout and (time.time() - last_speech) >= args.silence_timeout:
                log.info("silence timeout reached (%ds); ending", args.silence_timeout)
                break
            if args.once:
                break
            continue

        last_speech = time.time()

        try:
            text = provider.transcribe(audio, cfg.samplerate)
        except ProviderError as e:
            log.warning("transcribe failed: %s", e)
            if args.once: break
            continue
        if not text:
            log.info("chunk %d peak=%d empty transcript", chunks, peak)
            if args.once: break
            continue

        seg = Segment(segment_id=new_segment_id(), meeting_id=meeting_id,
                      started_at=_iso_now(), ended_at=_iso_now(),
                      text=text, model=cfg.transcription_model)
        outbox.enqueue(seg)
        log.info("chunk %d peak=%d text=%r", chunks, peak, text[:120])

        if vps is not None and not args.dry_run:
            _drain_outbox(outbox, vps, meeting_id, args.batch_size)

        if args.once:
            break

    if vps is not None and not args.dry_run:
        try:
            _drain_outbox(outbox, vps, meeting_id, args.batch_size)
        except Exception as e:
            log.warning("final drain error: %s", e)
    if vps is not None:
        vps.close()
    log.info("stats: %s", outbox.stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())

