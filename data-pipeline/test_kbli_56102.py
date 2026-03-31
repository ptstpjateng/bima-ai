"""
Quick test: scrape KBLI 56102 and print the structured result.

Run from data-pipeline/:
    python test_kbli_56102.py
"""

import asyncio
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

sys.path.insert(0, ".")
from extractors.oss_scraper import OssScraper


async def main() -> None:
    scraper = OssScraper(
        kbli_codes=["56102"],
        headless=True,
        save_json=True,
        vectorize=False,   # skip embedding — no sentence-transformers installed yet
    )

    report = await scraper.run()

    print("\n" + "=" * 60)
    print("SCRAPE REPORT")
    print("=" * 60)
    print(f"  Attempted : {report.kbli_codes_attempted}")
    print(f"  Succeeded : {report.success_count}")
    print(f"  Failed    : {report.failure_count}")

    if report.failed_codes:
        print("\nFAILURES:")
        for code, err in report.failed_codes.items():
            print(f"  {code}: {err}")

    for detail in report.results:
        print(f"\n{'=' * 60}")
        print(f"KBLI {detail.kbli_code}")
        print(f"URL  : {detail.detail_url}")
        print(f"\nURAIAN:\n{detail.uraian or '(not extracted)'}")

        print(f"\nRUANG LINGKUP ({len(detail.ruang_lingkup)} skala):")
        for scope in detail.ruang_lingkup:
            print(f"  [{scope.skala}]")
            print(f"    Luas Lahan         : {scope.luas_lahan or '-'}")
            print(f"    Tingkat Risiko     : {scope.tingkat_risiko or '-'}")
            print(f"    Perizinan Berusaha : {scope.perizinan_berusaha or '-'}")
            print(f"    Jangka Waktu       : {scope.jangka_waktu or '-'}")
            print(f"    PB UMKU            : {scope.pb_umku or '-'}")

        print(f"\nKEWAJIBAN ({len(detail.kewajiban_perizinan_berusaha)} item(s)):")
        for k in detail.kewajiban_perizinan_berusaha:
            print(f"  - {k}")

        print(f"\nPB UMKU DETAIL ({len(detail.pb_umku_detail)} item(s)):")
        for item in detail.pb_umku_detail:
            keterangan = f" — {item.keterangan}" if item.keterangan else ""
            print(f"  - {item.name}{keterangan}")

        # Also dump the raw JSON so we can inspect the full object
        print(f"\nRAW JSON OUTPUT:")
        print(json.dumps(json.loads(detail.model_dump_json()), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
