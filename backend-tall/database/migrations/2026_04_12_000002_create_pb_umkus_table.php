<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('pb_umkus', function (Blueprint $table) {
            $table->id();
            $table->string('nomeklatur')->index();
            $table->text('persyaratan')->nullable();
            $table->string('jangka_waktu_penerbitan')->nullable();
            $table->text('kewajiban')->nullable();
            $table->string('masa_berlaku')->nullable();
            $table->text('parameter')->nullable();
            $table->string('kewenangan')->nullable();
            $table->timestamps();
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('pb_umkus');
    }
};
