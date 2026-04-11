<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class Kbli extends Model
{
    protected $fillable = [
        'kode_kbli',
        'judul_kbli',
        'ruang_lingkup',
        'skala_usaha',
        'tingkat_risiko',
        'perizinan_berusaha',
        'persyaratan',
        'jangka_waktu_penerbitan',
        'kewajiban',
        'pb_umku_names',
        'parameter',
        'kewenangan',
        'sektor',
    ];
}
