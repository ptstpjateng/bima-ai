<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    /**
     * Run the migrations.
     *
     * Extends the base users table with MSME identity, business profile,
     * and role fields required by the BIMA-AI system.
     */
    public function up(): void
    {
        Schema::table('users', function (Blueprint $table) {
            // Role-based Access Control
            $table->enum('role', ['msme', 'dpmptsp_staff', 'admin'])
                ->default('msme')
                ->after('email');

            // Personal Identity
            $table->string('phone', 20)->nullable()
                ->after('role')
                ->comment('Primary contact number (used for WhatsApp/Telegram integration)');
            $table->string('nik', 16)->nullable()->unique()
                ->after('phone')
                ->comment('Nomor Induk Kependudukan — 16-digit Indonesian National ID');
            $table->string('npwp', 20)->nullable()->unique()
                ->after('nik')
                ->comment('Nomor Pokok Wajib Pajak — Indonesian Tax Identification Number');

            // Business Profile
            $table->string('business_name')->nullable()
                ->after('npwp');
            $table->enum('business_legal_type', ['perseorangan', 'cv', 'pt', 'ud', 'firma', 'koperasi', 'yayasan'])
                ->nullable()
                ->after('business_name')
                ->comment('Legal entity type of the business');
            $table->text('business_address')->nullable()
                ->after('business_legal_type');
            $table->string('province', 100)->nullable()
                ->after('business_address');
            $table->string('city', 100)->nullable()
                ->after('province')
                ->comment('Kabupaten or Kota');
            $table->string('district', 100)->nullable()
                ->after('city')
                ->comment('Kecamatan');
            $table->string('village', 100)->nullable()
                ->after('district')
                ->comment('Kelurahan or Desa');
            $table->string('postal_code', 10)->nullable()
                ->after('village');

            // Profile Photo
            $table->string('profile_photo_path')->nullable()
                ->after('postal_code');

            // Soft Deletes (allows safe user archival without data loss)
            $table->softDeletes()->after('updated_at');
        });
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::table('users', function (Blueprint $table) {
            $table->dropColumn([
                'role',
                'phone',
                'nik',
                'npwp',
                'business_name',
                'business_legal_type',
                'business_address',
                'province',
                'city',
                'district',
                'village',
                'postal_code',
                'profile_photo_path',
                'deleted_at',
            ]);
        });
    }
};
