<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('businesses', function (Blueprint $table) {
            $table->id();

            $table->foreignId('user_id')->constrained()->cascadeOnDelete();

            $table->string('name');
            $table->enum('legal_type', ['PT', 'CV', 'UD', 'Firma', 'Koperasi', 'Perorangan', 'Yayasan'])->default('Perorangan');
            $table->string('registration_number', 50)->nullable()->unique()->comment('NPWP / NIB utama usaha');

            // KBLI
            $table->string('primary_kbli_code', 10)->nullable();
            $table->string('primary_kbli_description')->nullable();
            $table->json('additional_kbli_codes')->nullable();

            // Scale & Finance
            $table->enum('scale', ['mikro', 'kecil', 'menengah', 'besar'])->default('mikro');
            $table->unsignedBigInteger('annual_revenue_estimate')->nullable();
            $table->unsignedInteger('employee_count')->nullable();

            // Location
            $table->text('address')->nullable();
            $table->string('province', 100)->nullable();
            $table->string('city', 100)->nullable();
            $table->string('district', 100)->nullable();
            $table->string('postal_code', 10)->nullable();

            $table->boolean('is_primary')->default(false)->comment('Primary business for this user');
            $table->json('metadata')->nullable();

            $table->timestamps();
            $table->softDeletes();

            $table->index(['user_id', 'is_primary']);
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('businesses');
    }
};
