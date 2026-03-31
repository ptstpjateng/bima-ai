<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('knowledge_base_articles', function (Blueprint $table) {
            $table->id();

            $table->string('title');
            $table->text('content')->comment('Full regulation text — fed into ChromaDB RAG');
            $table->enum('source_type', ['manual', 'pdf', 'url'])->default('manual');
            $table->string('pdf_path')->nullable()->comment('Storage path if uploaded via PDF');
            $table->string('source_url')->nullable();

            // Scope / tagging for RAG retrieval
            $table->string('region', 100)->nullable()->comment('e.g. Jawa Tengah, Semarang');
            $table->json('kbli_codes')->nullable()->comment('Applicable KBLI codes for targeted retrieval');
            $table->json('tags')->nullable();
            $table->enum('regulation_type', [
                'peraturan_pemerintah',
                'peraturan_daerah',
                'peraturan_menteri',
                'sop_dpmptsp',
                'panduan_oss',
                'umum',
            ])->default('umum');

            // Vectorization status
            $table->boolean('is_vectorized')->default(false);
            $table->timestamp('vectorized_at')->nullable();
            $table->string('chroma_doc_id', 100)->nullable()->unique();
            $table->text('vectorize_error')->nullable();

            // Publishing
            $table->foreignId('created_by')->nullable()->constrained('users')->nullOnDelete();
            $table->timestamp('published_at')->nullable();

            $table->timestamps();
            $table->softDeletes();

            $table->index(['is_vectorized', 'published_at']);
            $table->index(['regulation_type']);
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('knowledge_base_articles');
    }
};
