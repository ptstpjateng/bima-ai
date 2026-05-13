<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

/**
 * Captures the manually-applied production ALTER TABLE that added
 * `scraped_content` to `kbli_scrape_targets`. The data-pipeline scraper
 * (see CLAUDE.md "Pipeline worker (scraper)") writes the raw JSON payload
 * here before chunking it into ChromaDB. Without this migration,
 * `migrate:fresh` produces a schema where pipeline writes silently fail
 * with `column "scraped_content" does not exist`.
 */
return new class extends Migration
{
    public function up(): void
    {
        Schema::table('kbli_scrape_targets', function (Blueprint $table) {
            if (! Schema::hasColumn('kbli_scrape_targets', 'scraped_content')) {
                $table->text('scraped_content')->nullable()->after('scrape_error');
            }
        });
    }

    public function down(): void
    {
        Schema::table('kbli_scrape_targets', function (Blueprint $table) {
            if (Schema::hasColumn('kbli_scrape_targets', 'scraped_content')) {
                $table->dropColumn('scraped_content');
            }
        });
    }
};
