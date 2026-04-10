<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('pdf_parse_jobs', function (Blueprint $table) {
            $table->id();

            // Human-readable label set by the admin uploading the files
            $table->string('name');

            // Storage paths (relative to storage/app/) set by Filament FileUpload
            $table->string('kbli_pdf_path')->nullable()
                ->comment('Path within storage/app/ to the KBLI Detail PDF');
            $table->string('pb_umku_pdf_path')->nullable()
                ->comment('Path within storage/app/ to the PB-UMKU Detail PDF');

            // Optional filter: comma-separated KBLI codes to restrict processing.
            // Empty = process all KBLI codes found in the PDF.
            $table->string('kbli_codes_filter')->nullable()
                ->comment('Optional comma-separated KBLI codes to restrict extraction');

            // Pipeline lifecycle status
            $table->enum('status', ['pending', 'processing', 'completed', 'failed'])
                ->default('pending')
                ->index();

            // Progress tracking (0–100)
            $table->unsignedTinyInteger('progress_pct')->default(0);

            // Results
            $table->unsignedInteger('chunks_created')->default(0);
            $table->unsignedSmallInteger('kbli_processed')->default(0);

            // Raw merged JSON saved for admin review in the View page
            $table->longText('result_json')->nullable();

            // Error details when status = 'failed'
            $table->text('error_message')->nullable();

            // Audit fields
            $table->foreignId('created_by')->nullable()->constrained('users')->nullOnDelete();
            $table->timestamp('started_at')->nullable();
            $table->timestamp('completed_at')->nullable();

            $table->timestamps();

            $table->index(['status', 'created_at']);
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('pdf_parse_jobs');
    }
};
