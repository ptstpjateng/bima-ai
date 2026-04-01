"""
Dynamic OSS RBA pipeline runner for BIMA-AI.

Fetches pending KBLI targets from the Laravel backend API, scrapes each one
with Playwright, and reports back status + chunk count per code.

Environment (from .env or container environment):
    LARAVEL_BACKEND_URL   e.g. http://backend:80
    LARAVEL_API_KEY       must match backend INTERNAL_API_KEY

Run:
    python run_pipeline_dynamic.py [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(_PROJECT_ROOT / 'data' / 'pipeline_dynamic.log')),
    ],
)
logger = logging.getLogger(__name__)

from extractors.oss_scraper import OssScraper

BACKEND_URL = os.environ.get('LARAVEL_BACKEND_URL', 'http://backend:80')
API_KEY     = os.environ.get('LARAVEL_API_KEY', '')
HEADERS     = {'X-Internal-Key': API_KEY}


def _fetch_queue(limit: int) -> list[dict]:
    """Fetch up to `limit` pending KBLI codes from the backend."""
    url = f'{BACKEND_URL}/api/internal/pipeline/queue?limit={limit}'
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json().get('data', {}).get('targets', [])
    except Exception as exc:
        logger.error('Failed to fetch queue from backend: %s', exc)
        return []


def _report_status(kbli_code: str, status: str, chunks: int = 0, duration: int = 0, error: str = '') -> None:
    """Report scrape result back to the backend pipeline status endpoint."""
    url = f'{BACKEND_URL}/api/internal/pipeline/status'
    payload = {
        'kbli_code':        kbli_code,
        'status':           status,
        'chunks_upserted':  chunks,
        'duration_seconds': duration,
        'error':            error or None,
    }
    try:
        resp = httpx.post(url, json=payload, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        logger.info('Reported %s → %s (%d chunks)', kbli_code, status, chunks)
    except Exception as exc:
        logger.warning('Failed to report status for %s: %s', kbli_code, exc)


async def main(limit: int) -> int:
    logger.info('=' * 60)
    logger.info('BIMA-AI Dynamic Pipeline Starting (limit=%d)', limit)
    logger.info('Backend: %s', BACKEND_URL)
    logger.info('=' * 60)

    targets = _fetch_queue(limit)
    if not targets:
        logger.info('No pending targets found. Exiting.')
        return 0

    codes = [t['kbli_code'] for t in targets]
    logger.info('Fetched %d targets: %s', len(codes), ', '.join(codes))

    # Mark each as scraping before we start
    for code in codes:
        _report_status(code, 'scraping')

    # Scrape each code individually so we can report per-code status
    success_count = 0
    for target in targets:
        code = target['kbli_code']
        logger.info('Scraping KBLI: %s (%s)', code, target.get('kbli_description', ''))
        t_start = time.monotonic()

        try:
            scraper = OssScraper(kbli_codes=[code], headless=True, save_json=True, vectorize=True)
            report  = await scraper.run()

            duration = int(time.monotonic() - t_start)

            if report.success_count > 0:
                detail = report.results[0]
                # Estimate chunks: uraian + ruang_lingkup + kewajiban + pb_umku
                est_chunks = (
                    (1 if detail.uraian else 0)
                    + len(detail.ruang_lingkup)
                    + (1 if detail.kewajiban_perizinan_berusaha else 0)
                    + len(detail.pb_umku_detail)
                )
                _report_status(code, 'done', chunks=est_chunks, duration=duration)
                success_count += 1
            else:
                err = report.failed_codes.get(code, 'Unknown scrape failure')
                _report_status(code, 'failed', error=err, duration=duration)

        except Exception as exc:
            duration = int(time.monotonic() - t_start)
            logger.error('Unhandled error for KBLI %s: %s', code, exc, exc_info=True)
            _report_status(code, 'failed', error=str(exc)[:500], duration=duration)

    logger.info('=' * 60)
    logger.info('PIPELINE COMPLETE — succeeded=%d / %d', success_count, len(codes))
    logger.info('=' * 60)
    return 0 if success_count > 0 else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=10, help='Max KBLI codes per run')
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.limit)))
