#!/usr/bin/env python3
"""Explicit live ACP text/image/cancellation probe, restricted to free Zen IDs."""
import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from preprocessing.contracts import default_profile
from preprocessing.engine import PINNED_VERSION, OwnedEngine
from preprocessing.errors import ServiceError

FREE_MODELS = ('opencode/space-bunny-free', 'opencode/longcat-2.5-preview-free')


async def probe(args):
    from PIL import Image, ImageDraw, ImageFont

    root = Path(tempfile.mkdtemp(prefix='preprocessing-acp-probe-', dir=args.temp_parent))
    image_path = root / 'test.png'
    image = Image.new('RGB', (440, 160), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=38)
    draw.text((20, 20), 'SAMPLE 42', fill='black', font=font)
    draw.text((20, 85), 'AXIS 17', fill='black', font=font)
    image.save(image_path)
    profile = default_profile()
    profile['models']['vision'] = args.model
    profile['limits']['modelSeconds'] = args.seconds
    profile['retry'] = {'attempts': 1, 'noProgress': 1}
    engine = OwnedEngine(root / 'engine', executable=args.executable, input_roots=[root],
                         rpc_seconds=60, cancel_seconds=15)
    report = {'cliPin': PINNED_VERSION, 'model': args.model, 'stateDirectory': str(root),
              'credentials': 'isolated; no inherited provider credentials', 'text': None, 'vision': None,
              'cancellation': None, 'selection': []}
    request = engine._request

    async def observe(method, params):
        if method != 'session/set_config_option':
            response = await request(method, params)
            if method == 'session/new':
                report['availableModes'] = [o for o in response.get('configOptions', []) if o.get('id') == 'mode']
            return response
        observation = {'configId': params['configId'], 'requested': params['value']}
        report['selection'].append(observation)
        try:
            response = await request(method, params)
        except Exception:
            observation['rejected'] = True
            raise
        observation['currentValues'] = {o['id']: o.get('currentValue') for o in response.get('configOptions', [])}
        return response

    engine._request = observe
    if args.diagnostics:
        async def capture_stderr():
            captured = bytearray()
            while chunk := await engine.process.stderr.read(8192):
                if len(captured) < 65536:
                    captured.extend(chunk)
            report['diagnostics'] = captured.decode(errors='replace')
        engine._drain_stderr = capture_stderr
    started = time.monotonic()

    async def text_probe(job, expected):
        try:
            result = await engine.start(job, job, 'probe-generation', profile, [],
                {'kind': 'probe', 'prompt': f'Return exactly this text and nothing else: {expected}'})
            return {'result': result, 'matched': result['text'].strip() == expected}
        except ServiceError as error:
            return {'error': error.as_dict(), 'sessionId': getattr(error, 'session_id', None),
                    'settlement': getattr(error, 'settlement', None)}

    try:
        report['text'] = await text_probe('probe-text', 'TEXT CHECK 31')
        if report['text'].get('matched'):
            try:
                result = await engine.start('probe-vision', 'probe-vision', 'probe-generation', profile,
                    [{'path': str(image_path), 'mediaType': 'image/png'}],
                    {'kind': 'ocr', 'sourceUnit': 1,
                     'prompt': 'Transcribe the two lines visible in the attached image. Return only their text.'})
                report['vision'] = {'result': result, 'matched': all(s in result['text'] for s in ('SAMPLE 42', 'AXIS 17'))}
            except ServiceError as error:
                report['vision'] = {'error': error.as_dict(), 'sessionId': getattr(error, 'session_id', None),
                                    'settlement': getattr(error, 'settlement', None)}
        if args.cancel and (report['vision'] or {}).get('matched'):
            pid = engine.process.pid
            task = asyncio.create_task(engine.start('probe-cancel', 'probe-cancel', 'probe-generation',
                profile, [], {'kind': 'probe', 'prompt': 'Write a detailed 4000-word explanation of optical character recognition.'}))
            async with asyncio.timeout(65):
                while not task.done():
                    op = engine._active.get('probe-cancel')
                    if op and op.prompt is not None:
                        break
                    await asyncio.sleep(.01)
            await asyncio.sleep(.25)
            pending_at_cancel = not task.done()
            settlement = await engine.cancel('probe-cancel')
            try:
                result = await task
                report['cancellation'] = {'settlement': settlement, 'completedBeforeCancel': True,
                                           'sessionId': result['sessionId']}
            except ServiceError as error:
                report['cancellation'] = {'settlement': settlement, 'error': error.as_dict(),
                                           'pendingAtCancel': pending_at_cancel}
            report['afterCancel'] = await text_probe('probe-after-cancel', 'AFTER CANCEL 73')
            report['cancellation']['sameProcess'] = engine.process.pid == pid and engine.process.returncode is None
    finally:
        await engine.close()
        report['seconds'] = round(time.monotonic() - started, 3)
        report['acpExitCode'] = engine.process.returncode if engine.process else None
    print(json.dumps(report, indent=2))
    passed = report['text'].get('matched') and (report['vision'] or {}).get('matched')
    if args.cancel:
        cancellation = report['cancellation'] or {}
        passed = (passed and cancellation.get('pendingAtCancel') and cancellation.get('sameProcess')
                  and cancellation.get('error', {}).get('code') == 'RUNTIME_INTERRUPTED'
                  and cancellation.get('settlement', {}).get('settled')
                  and cancellation.get('settlement', {}).get('sessionClosed')
                  and report.get('afterCancel', {}).get('matched'))
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True, help='Path to isolated @opencode/cli 2.0.19 executable')
    parser.add_argument('--model', choices=FREE_MODELS, default=FREE_MODELS[0])
    parser.add_argument('--seconds', type=float, default=90)
    parser.add_argument('--temp-parent', default='/tmp/opencode')
    parser.add_argument('--cancel', action='store_true', help='Also probe scoped prompt cancellation after vision success')
    parser.add_argument('--diagnostics', action='store_true', help='Include isolated probe engine stderr')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(probe(args)))


if __name__ == '__main__':
    main()
