#!/usr/bin/env python3
"""Live HTTP text/image/cancellation probe with service-backed persistence."""
import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

from preprocessing.cli import load_descriptor, load_profile
from preprocessing.engine import OpenCodeServerEngine
from preprocessing.errors import ServiceError
from preprocessing.service import Service, now


async def probe(args):
    from PIL import Image, ImageDraw, ImageFont

    root = args.data_root.resolve()
    profile = load_profile(model=args.model)
    profile['limits']['modelSeconds'] = args.seconds
    engine = OpenCodeServerEngine(load_descriptor(args.engine_descriptor),
                                  input_roots=[root / '.preprocessing/jobs'])
    service = Service(root, root, profile=profile, engine=engine)
    report = {'engineVersion': engine.descriptor['version'], 'model': args.model, 'dataRoot': str(root),
              'text': None, 'vision': None, 'cancellation': None}
    started = time.monotonic()

    def new_job(label):
        # Synthetic jobs exercise Service's actual model/persistence path, not a parser.
        job_id = 'job_' + uuid.uuid4().hex
        job = {'schemaVersion': 1, 'id': job_id, 'projectId': service.store.scope,
               'submissionKey': str(uuid.uuid4()), 'revision': 1, 'state': 'running',
               'createdAt': now(), 'startedAt': now(), 'finishedAt': None,
               'profile': service.profile, 'source': None, 'documents': None,
               'usage': None, 'usageStatus': 'unavailable', 'error': None,
               'probe': label}
        service.store.insert(job, json.dumps({'probe': label}))
        staging = service.private / 'jobs' / job_id / 'staging'
        staging.mkdir(parents=True)
        return job_id, staging

    async def run_operation(job_id, staging, prompt, refs=()):
        service.active = job_id
        response = None
        try:
            response = await service._model(job_id, staging, {
                'operation': {'kind': 'probe', 'prompt': prompt}, 'inputRefs': list(refs)})
            return response
        except ServiceError as error:
            response = {'error': error.as_dict()}
            return response
        finally:
            job = service.get(job_id)
            if job['state'] == 'running':
                job.update(state='succeeded' if response and 'result' in response else 'failed',
                           finishedAt=now(), error=(response or {}).get('error'))
                service.store.save(job)
            service.active = None

    async def text_probe(label, expected):
        job_id, staging = new_job(label)
        response = await run_operation(job_id, staging,
                                       f'Return exactly this text and nothing else: {expected}')
        return {**response, 'jobId': job_id,
                'matched': response.get('result', {}).get('text', '').strip() == expected}

    try:
        await service.start()
        report['dependency'] = service.capabilities()['dependency']
        if not report['dependency']['ready'] or service.settlement_unknown:
            return 1, report
        report['text'] = await text_probe('text', 'TEXT CHECK 31')
        if report['text']['matched']:
            job_id, staging = new_job('vision')
            image_path = staging / 'test.png'
            with Image.new('RGB', (440, 160), 'white') as image:
                draw = ImageDraw.Draw(image)
                font = ImageFont.load_default(size=38)
                draw.text((20, 20), 'SAMPLE 42', fill='black', font=font)
                draw.text((20, 85), 'AXIS 17', fill='black', font=font)
                image.save(image_path)
            response = await run_operation(job_id, staging,
                'Transcribe the two lines visible in the attached image. Return only their text.',
                [{'path': str(image_path), 'mediaType': 'image/png'}])
            report['vision'] = {**response, 'jobId': job_id, 'matched': all(
                line in response.get('result', {}).get('text', '') for line in ('SAMPLE 42', 'AXIS 17'))}
        if args.cancel and (report['vision'] or {}).get('matched'):
            job_id, staging = new_job('cancel')
            task = asyncio.create_task(run_operation(job_id, staging,
                'Write a detailed 4000-word explanation of optical character recognition.'))
            try:
                async with asyncio.timeout(engine.rpc_seconds * 4 + 5):
                    while not task.done():
                        if any(item.get('admissionAcknowledged') for item in service.store.operations(job_id)):
                            break
                        await asyncio.sleep(.01)
                await asyncio.sleep(.25)
                pending = not task.done()
                await service.cancel(job_id)
                response = await task
                records = service.store.operations(job_id)
                settlement = records[-1].get('settlement', {}) if records else {}
                report['cancellation'] = {**response, 'jobId': job_id,
                                          'pendingAtCancel': pending, 'settlement': settlement}
                if settlement.get('settled') and settlement.get('sessionClosed'):
                    report['afterCancel'] = await text_probe('after-cancel', 'AFTER CANCEL 73')
            finally:
                if not task.done():
                    await service.cancel(job_id)
                    await task
        passed = (report['text'] or {}).get('matched') and (report['vision'] or {}).get('matched')
        if args.cancel:
            cancellation = report['cancellation'] or {}
            passed = (passed and cancellation.get('pendingAtCancel')
                      and cancellation.get('error', {}).get('code') == 'RUNTIME_INTERRUPTED'
                      and cancellation.get('settlement', {}).get('settled')
                      and cancellation.get('settlement', {}).get('sessionClosed')
                      and report.get('afterCancel', {}).get('matched'))
        return (0 if passed else 1), report
    except ServiceError as error:
        report['error'] = error.as_dict()
        return 1, report
    except TimeoutError:
        report['error'] = {'code': 'PROBE_TIMEOUT', 'message': 'Cancellation admission was not observed before the probe deadline.'}
        return 1, report
    finally:
        try:
            await service.close()
        finally:
            report['seconds'] = round(time.monotonic() - started, 3)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-descriptor', type=Path,
                        help='Private descriptor JSON; defaults to PREPROCESSING_ENGINE_DESCRIPTOR.')
    parser.add_argument('--data-root', type=Path, required=True,
                        help='Dedicated probe service root watched by the engine owner.')
    parser.add_argument('--model', required=True, help='Explicit enabled provider/model with image capability.')
    parser.add_argument('--seconds', type=int, default=90, help='Positive per-prompt deadline in seconds.')
    parser.add_argument('--cancel', action='store_true', help='Also check scoped cancellation and post-cancel reuse.')
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        parser.error('--seconds must be positive')
    try:
        code, report = asyncio.run(probe(args))
    except (ServiceError, OSError) as error:
        report = {'error': error.as_dict() if isinstance(error, ServiceError) else
                  {'code': 'IO_ERROR', 'message': 'Could not access probe configuration or state.'}}
        code = 1
    print(json.dumps(report, indent=2, allow_nan=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
