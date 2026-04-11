<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class PbUmku extends Model
{
    protected $fillable = [
        'nomeklatur',
        'persyaratan',
        'jangka_waktu_penerbitan',
        'kewajiban',
        'masa_berlaku',
        'parameter',
        'kewenangan',
    ];
}
