<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('kbli_scrape_targets', function (Blueprint $table) {
            $table->id();

            $table->string('kbli_code', 10)->unique();
            $table->string('kbli_description')->nullable();
            $table->string('sector', 100)->nullable();  // e.g. "Food & Beverage", "Health"

            $table->enum('status', ['pending', 'queued', 'scraping', 'done', 'failed'])
                ->default('pending')
                ->index();

            $table->smallInteger('priority')->default(0); // higher = scraped sooner

            // Results from last successful scrape
            $table->timestamp('last_scraped_at')->nullable();
            $table->unsignedInteger('scrape_duration_seconds')->nullable();
            $table->unsignedInteger('chroma_chunks')->default(0);
            $table->text('scrape_error')->nullable();

            $table->foreignId('created_by')
                ->nullable()
                ->constrained('users')
                ->nullOnDelete();

            $table->timestamps();
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('kbli_scrape_targets');
    }
};
