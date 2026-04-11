<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('kblis', function (Blueprint $table) {
            $table->id();
            $table->string('kode_kbli', 10)->index();
            $table->string('judul_kbli');
            $table->text('ruang_lingkup')->nullable();
            $table->string('skala_usaha')->nullable();
            $table->string('tingkat_risiko')->nullable();
            $table->text('perizinan_berusaha')->nullable();
            $table->text('persyaratan')->nullable();
            $table->string('jangka_waktu_penerbitan')->nullable();
            $table->text('kewajiban')->nullable();
            $table->text('pb_umku_names')->nullable();
            $table->text('parameter')->nullable();
            $table->string('kewenangan')->nullable();
            $table->timestamps();

            $table->index(['kode_kbli', 'skala_usaha']);
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('kblis');
    }
};
