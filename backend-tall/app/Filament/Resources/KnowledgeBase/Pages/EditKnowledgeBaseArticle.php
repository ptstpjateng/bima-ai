<?php

namespace App\Filament\Resources\KnowledgeBase\Pages;

use App\Filament\Resources\KnowledgeBase\KnowledgeBaseResource;
use App\Models\KnowledgeBaseArticle;
use App\Services\VectorizeService;
use Filament\Actions\Action;
use Filament\Actions\DeleteAction;
use Filament\Notifications\Notification;
use Filament\Resources\Pages\EditRecord;

class EditKnowledgeBaseArticle extends EditRecord
{
    protected static string $resource = KnowledgeBaseResource::class;

    protected function getHeaderActions(): array
    {
        return [
            Action::make('sync_to_ai')
                ->label('Sync ke AI Brain')
                ->icon('heroicon-o-cpu-chip')
                ->color('success')
                ->requiresConfirmation()
                ->modalHeading('Sync Konten ke ChromaDB?')
                ->modalDescription('Embedding lama akan diganti dengan konten terbaru yang tersimpan.')
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
                    } else {
                        Notification::make()
                            ->danger()
                            ->title('Sinkronisasi gagal')
                            ->body($result['error'] ?? 'Periksa log server.')
                            ->send();
                    }
                }),

            DeleteAction::make(),
        ];
    }
}
