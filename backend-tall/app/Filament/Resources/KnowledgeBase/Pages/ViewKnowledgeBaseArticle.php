<?php

namespace App\Filament\Resources\KnowledgeBase\Pages;

use App\Filament\Resources\KnowledgeBase\Infolists\KnowledgeBaseInfolist;
use App\Filament\Resources\KnowledgeBase\KnowledgeBaseResource;
use App\Models\KnowledgeBaseArticle;
use App\Services\VectorizeService;
use Filament\Actions\Action;
use Filament\Actions\EditAction;
use Filament\Notifications\Notification;
use Filament\Resources\Pages\ViewRecord;
use Filament\Schemas\Schema;

class ViewKnowledgeBaseArticle extends ViewRecord
{
    protected static string $resource = KnowledgeBaseResource::class;

    public function infolist(Schema $schema): Schema
    {
        return KnowledgeBaseInfolist::configure($schema);
    }

    protected function getHeaderActions(): array
    {
        return [
            Action::make('sync_to_ai')
                ->label('Sync ke AI Brain')
                ->icon('heroicon-o-cpu-chip')
                ->color('success')
                ->requiresConfirmation()
                ->modalHeading('Sync Konten ke ChromaDB?')
                ->modalDescription('Embedding lama akan diganti dengan konten artikel ini.')
                ->action(function () {
                    /** @var KnowledgeBaseArticle $record */
                    $record = $this->getRecord();
                    $result = (new VectorizeService())->sync($record);
                    if ($result['success']) {
                        Notification::make()
                            ->success()
                            ->title('Berhasil disinkronkan ke AI Brain!')
                            ->body("Doc ID: {$result['doc_id']}")
                            ->send();
                        $this->refreshFormData(['is_vectorized', 'vectorized_at', 'chroma_doc_id', 'vectorize_error']);
                    } else {
                        Notification::make()
                            ->danger()
                            ->title('Sinkronisasi gagal')
                            ->body($result['error'] ?? 'Periksa log server.')
                            ->send();
                    }
                }),

            EditAction::make(),
        ];
    }
}
