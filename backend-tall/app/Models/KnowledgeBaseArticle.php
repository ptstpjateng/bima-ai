<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\SoftDeletes;

class KnowledgeBaseArticle extends Model
{
    use HasFactory, SoftDeletes;

    protected $fillable = [
        'title',
        'content',
        'source_type',
        'pdf_path',
        'source_url',
        'region',
        'kbli_codes',
        'tags',
        'regulation_type',
        'is_vectorized',
        'vectorized_at',
        'chroma_doc_id',
        'vectorize_error',
        'created_by',
        'published_at',
    ];

    protected $casts = [
        'kbli_codes'     => 'array',
        'tags'           => 'array',
        'is_vectorized'  => 'boolean',
        'vectorized_at'  => 'datetime',
        'published_at'   => 'datetime',
    ];

    public function author(): BelongsTo
    {
        return $this->belongsTo(User::class, 'created_by');
    }
}
