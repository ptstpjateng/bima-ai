<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    /**
     * Run the migrations.
     *
     * Tracks every OSS RBA permit application submitted by an MSME.
     * Mirrors the OSS (Online Single Submission) risk-based approach tiers.
     */
    public function up(): void
    {
        Schema::create('permit_applications', function (Blueprint $table) {
            $table->id();

            // Ownership & Assignment
            $table->foreignId('user_id')
                ->constrained()
                ->cascadeOnDelete()
                ->comment('The MSME owner who submitted this application');
            $table->foreignId('reviewer_id')
                ->nullable()
                ->constrained('users')
                ->nullOnDelete()
                ->comment('DPMPTSP staff assigned to review this application');

            // Tracking Numbers
            $table->string('application_number', 30)->unique()->comment('Internal BIMA-AI reference number (e.g. BIMA-2026-00001)');
            $table->string('oss_application_number', 50)->nullable()->unique()->comment('OSS RBA official application number');
            $table->string('nib', 20)->nullable()->unique()->comment('Nomor Induk Berusaha — issued upon approval');

            // KBLI Classification (Indonesian Standard Business Classification)
            $table->string('kbli_code', 10)->comment('5-digit KBLI code, e.g. 56101');
            $table->string('kbli_description')->comment('Human-readable KBLI category name');
            $table->string('kbli_section', 100)->nullable()->comment('KBLI section/sector grouping');

            // OSS RBA Risk & Scale Classification
            $table->enum('business_scale', ['mikro', 'kecil', 'menengah', 'besar'])
                ->default('mikro')
                ->comment('Business scale per OSS RBA criteria');
            $table->enum('risk_level', ['rendah', 'menengah_rendah', 'menengah_tinggi', 'tinggi'])
                ->default('rendah')
                ->comment('OSS RBA risk tier determining required permits');

            // Application Status Lifecycle
            $table->enum('status', [
                'draft',                   // Saved but not yet submitted
                'submitted',               // Submitted, awaiting initial review
                'under_review',            // Actively being reviewed by DPMPTSP
                'additional_docs_required',// Reviewer requested more documents
                'approved',                // Permit granted
                'rejected',                // Permit denied
                'expired',                 // Permit has passed expiry date
            ])->default('draft');

            // Business Location (may differ from user's registered address)
            $table->text('business_location_address')->nullable();
            $table->string('business_location_province', 100)->nullable();
            $table->string('business_location_city', 100)->nullable();

            // Financial Thresholds (used to determine scale/risk)
            $table->unsignedBigInteger('annual_revenue_estimate')->nullable()->comment('Estimated annual revenue in IDR');
            $table->unsignedInteger('employee_count')->nullable();

            // Documents & Requirements
            $table->json('documents')->nullable()->comment('Array of uploaded document metadata {name, path, type, uploaded_at}');
            $table->json('requirements_checklist')->nullable()->comment('Checklist of required items and their completion status');

            // Review & Notes
            $table->text('reviewer_notes')->nullable();
            $table->text('applicant_notes')->nullable()->comment('Notes from the applicant during submission');

            // Key Dates
            $table->timestamp('submitted_at')->nullable();
            $table->timestamp('reviewed_at')->nullable();
            $table->timestamp('approved_at')->nullable();
            $table->timestamp('rejected_at')->nullable();
            $table->date('permit_expiry_date')->nullable()->comment('Date the issued permit expires');

            // Extra
            $table->json('metadata')->nullable()->comment('Flexible storage for future OSS API response data');

            $table->timestamps();
            $table->softDeletes();

            // Indexes for common filter queries
            $table->index(['status', 'risk_level']);
            $table->index(['kbli_code']);
            $table->index(['submitted_at']);
        });
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('permit_applications');
    }
};
