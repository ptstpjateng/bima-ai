<?php

namespace App\Filament\Pages;

use App\Models\Kbli;
use App\Models\PbUmku;
use BackedEnum;
use Filament\Actions\Action;
use Filament\Forms\Components\FileUpload;
use Filament\Notifications\Notification;
use Filament\Pages\Page;
use Filament\Support\Icons\Heroicon;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use PhpOffice\PhpSpreadsheet\IOFactory;

class DataImportHub extends Page
{
    protected static string|BackedEnum|null $navigationIcon = Heroicon::OutlinedArrowUpTray;

    protected static string|\UnitEnum|null $navigationGroup = 'Data Hub';

    protected static ?string $navigationLabel = 'Import Data';

    protected static ?int $navigationSort = 3;

    protected static ?string $title = 'Data Import Hub';

    protected static ?string $slug = 'data-import-hub';

    protected string $view = 'filament.pages.data-import-hub';

    protected function getHeaderActions(): array
    {
        return [
            Action::make('importKbli')
                ->label('Upload KBLI Excel')
                ->icon('heroicon-o-document-arrow-up')
                ->color('primary')
                ->form([
                    FileUpload::make('kbli_file')
                        ->label('File KBLI (.xlsx)')
                        ->acceptedFileTypes(['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'])
                        ->required()
                        ->disk('local')
                        ->directory('imports'),
                ])
                ->action(function (array $data): void {
                    $this->processKbliImport($data['kbli_file']);
                }),

            Action::make('importPbUmku')
                ->label('Upload PB UMKU Excel')
                ->icon('heroicon-o-document-arrow-up')
                ->color('warning')
                ->form([
                    FileUpload::make('pb_umku_file')
                        ->label('File PB UMKU (.xlsx)')
                        ->acceptedFileTypes(['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'])
                        ->required()
                        ->disk('local')
                        ->directory('imports'),
                ])
                ->action(function (array $data): void {
                    $this->processPbUmkuImport($data['pb_umku_file']);
                }),

            Action::make('syncChromaDb')
                ->label('Sync ke ChromaDB')
                ->icon('heroicon-o-arrow-path')
                ->color('success')
                ->requiresConfirmation()
                ->modalHeading('Rebuild ChromaDB?')
                ->modalDescription('Ini akan menjalankan ETL pipeline untuk membangun ulang koleksi oss_regulations di ChromaDB dari data PostgreSQL saat ini.')
                ->action(function (): void {
                    $this->triggerChromaSync();
                }),
        ];
    }

    private function processKbliImport(string $filePath): void
    {
        $fullPath = storage_path('app/private/' . $filePath);

        if (!file_exists($fullPath)) {
            Notification::make()->danger()->title('File tidak ditemukan')->send();
            return;
        }

        try {
            $spreadsheet = IOFactory::load($fullPath);
            $sheet = $spreadsheet->getActiveSheet();
            $rows = $sheet->toArray(null, true, true, true);

            // Find header row and detect column mapping
            $headers = [];
            $dataStartRow = 1;
            foreach ($rows as $i => $row) {
                $firstCell = trim((string) ($row['A'] ?? ''));
                if (str_contains(strtolower($firstCell), 'no') || $firstCell === 'No') {
                    $headers = array_map(fn ($v) => trim((string) $v), $row);
                    $dataStartRow = $i + 1;
                    break;
                }
            }

            if (empty($headers)) {
                // Assume first row is header
                $headers = array_map(fn ($v) => trim((string) $v), $rows[1]);
                $dataStartRow = 2;
            }

            // Skip numeric sub-header row (column numbers)
            if (isset($rows[$dataStartRow])) {
                $testRow = $rows[$dataStartRow];
                $testVal = $testRow['B'] ?? null;
                if (is_numeric($testVal) && (int) $testVal < 100) {
                    $dataStartRow++;
                }
            }

            // Map column letters to field names
            $colMap = [];
            $fieldNames = [
                'No' => 'no', 'Kode KBLI' => 'kode_kbli', 'Judul KBLI' => 'judul_kbli',
                'Ruang Lingkup' => 'ruang_lingkup', 'Skala Usaha' => 'skala_usaha',
                'Tingkat Risiko' => 'tingkat_risiko', 'Perizinan Berusaha' => 'perizinan_berusaha',
                'Persyaratan' => 'persyaratan', 'Jangka Waktu Penerbitan' => 'jangka_waktu_penerbitan',
                'Kewajiban' => 'kewajiban', 'PB UMKU' => 'pb_umku_names',
                'Parameter' => 'parameter', 'Kewenangan' => 'kewenangan',
                'Sektor' => 'sektor',
            ];
            foreach ($headers as $col => $header) {
                if (isset($fieldNames[$header])) {
                    $colMap[$fieldNames[$header]] = $col;
                }
            }

            // Truncate and re-import
            DB::transaction(function () use ($rows, $dataStartRow, $colMap) {
                Kbli::truncate();

                $lastKode = '';
                $lastJudul = '';
                $lastRuangLingkup = '';
                $lastSektor = '';
                $count = 0;

                foreach ($rows as $i => $row) {
                    if ($i < $dataStartRow) continue;

                    // Forward fill for merged cells
                    $kode = trim((string) ($row[$colMap['kode_kbli'] ?? 'B'] ?? ''));
                    if ($kode !== '' && !is_numeric($kode)) {
                        // Non-numeric, skip
                    } elseif ($kode !== '') {
                        $lastKode = str_pad((string) (int) $kode, 5, '0', STR_PAD_LEFT);
                    }
                    $kode = $lastKode;

                    $judul = trim((string) ($row[$colMap['judul_kbli'] ?? 'C'] ?? ''));
                    if ($judul !== '') $lastJudul = $judul;
                    else $judul = $lastJudul;

                    $ruangLingkup = trim((string) ($row[$colMap['ruang_lingkup'] ?? 'D'] ?? ''));
                    if ($ruangLingkup !== '') $lastRuangLingkup = $ruangLingkup;
                    else $ruangLingkup = $lastRuangLingkup;

                    $sektor = trim((string) ($row[$colMap['sektor'] ?? 'N'] ?? ''));
                    if ($sektor !== '') $lastSektor = $sektor;
                    else $sektor = $lastSektor;

                    $skala = trim((string) ($row[$colMap['skala_usaha'] ?? 'E'] ?? ''));
                    if ($skala === '' && $kode === '') continue;

                    // Expand multi-value skala usaha
                    $skalaValues = array_filter(array_map('trim', preg_split('/[\r\n]+/', $skala)));
                    if (empty($skalaValues)) $skalaValues = [''];

                    foreach ($skalaValues as $skalaVal) {
                        if ($kode === '') continue;

                        Kbli::create([
                            'kode_kbli' => $kode,
                            'judul_kbli' => $judul,
                            'ruang_lingkup' => $ruangLingkup,
                            'skala_usaha' => $skalaVal,
                            'tingkat_risiko' => trim((string) ($row[$colMap['tingkat_risiko'] ?? 'F'] ?? '')),
                            'perizinan_berusaha' => trim((string) ($row[$colMap['perizinan_berusaha'] ?? 'G'] ?? '')),
                            'persyaratan' => trim((string) ($row[$colMap['persyaratan'] ?? 'H'] ?? '')),
                            'jangka_waktu_penerbitan' => trim((string) ($row[$colMap['jangka_waktu_penerbitan'] ?? 'I'] ?? '')),
                            'kewajiban' => trim((string) ($row[$colMap['kewajiban'] ?? 'J'] ?? '')),
                            'pb_umku_names' => trim((string) ($row[$colMap['pb_umku_names'] ?? 'K'] ?? '')),
                            'parameter' => trim((string) ($row[$colMap['parameter'] ?? 'L'] ?? '')),
                            'kewenangan' => trim((string) ($row[$colMap['kewenangan'] ?? 'M'] ?? '')),
                            'sektor' => $sektor,
                        ]);
                        $count++;
                    }
                }

                Log::info("KBLI import complete: {$count} records inserted");
            });

            $total = Kbli::count();
            $unique = Kbli::distinct('kode_kbli')->count('kode_kbli');

            Notification::make()
                ->success()
                ->title('Import KBLI Berhasil')
                ->body("Total: {$total} record | {$unique} kode KBLI unik")
                ->send();

        } catch (\Throwable $e) {
            Log::error("KBLI import failed: " . $e->getMessage());
            Notification::make()
                ->danger()
                ->title('Import Gagal')
                ->body($e->getMessage())
                ->send();
        } finally {
            @unlink($fullPath);
        }
    }

    private function processPbUmkuImport(string $filePath): void
    {
        $fullPath = storage_path('app/private/' . $filePath);

        if (!file_exists($fullPath)) {
            Notification::make()->danger()->title('File tidak ditemukan')->send();
            return;
        }

        try {
            $spreadsheet = IOFactory::load($fullPath);
            $sheet = $spreadsheet->getActiveSheet();
            $rows = $sheet->toArray(null, true, true, true);

            // Find data start (skip header + numeric sub-header)
            $dataStartRow = 1;
            foreach ($rows as $i => $row) {
                $firstCell = trim((string) ($row['A'] ?? ''));
                if (str_contains(strtolower($firstCell), 'no') || $firstCell === 'No') {
                    $dataStartRow = $i + 1;
                    break;
                }
            }

            // Skip numeric sub-header
            if (isset($rows[$dataStartRow])) {
                $testVal = $rows[$dataStartRow]['B'] ?? null;
                if (is_numeric($testVal) && (int) $testVal < 100) {
                    $dataStartRow++;
                }
            }

            DB::transaction(function () use ($rows, $dataStartRow) {
                PbUmku::truncate();

                $lastNomeklatur = '';
                $count = 0;

                foreach ($rows as $i => $row) {
                    if ($i < $dataStartRow) continue;

                    $nomeklatur = trim((string) ($row['B'] ?? ''));

                    // Skip section headers (Roman numeral patterns)
                    if (preg_match('/^[IVX]+\.\s/', $nomeklatur)) continue;

                    // Forward fill
                    if ($nomeklatur !== '') {
                        // Clean asterisk prefix and trailing notes
                        $nomeklatur = preg_replace('/^\*/', '', $nomeklatur);
                        $nomeklatur = explode("\n", $nomeklatur)[0];
                        $nomeklatur = trim($nomeklatur);
                        $lastNomeklatur = $nomeklatur;
                    } else {
                        $nomeklatur = $lastNomeklatur;
                    }

                    if ($nomeklatur === '') continue;

                    $persyaratan = trim((string) ($row['C'] ?? ''));
                    if ($persyaratan === '') continue; // Skip rows with no actual data

                    PbUmku::create([
                        'nomeklatur' => $nomeklatur,
                        'persyaratan' => $persyaratan,
                        'jangka_waktu_penerbitan' => trim((string) ($row['D'] ?? '')),
                        'kewajiban' => trim((string) ($row['E'] ?? '')),
                        'masa_berlaku' => trim((string) ($row['F'] ?? '')),
                        'parameter' => trim((string) ($row['G'] ?? '')),
                        'kewenangan' => trim((string) ($row['H'] ?? '')),
                    ]);
                    $count++;
                }

                Log::info("PB UMKU import complete: {$count} records inserted");
            });

            $total = PbUmku::count();

            Notification::make()
                ->success()
                ->title('Import PB UMKU Berhasil')
                ->body("Total: {$total} record PB UMKU")
                ->send();

        } catch (\Throwable $e) {
            Log::error("PB UMKU import failed: " . $e->getMessage());
            Notification::make()
                ->danger()
                ->title('Import Gagal')
                ->body($e->getMessage())
                ->send();
        } finally {
            @unlink($fullPath);
        }
    }

    private function triggerChromaSync(): void
    {
        try {
            $pipelineUrl = 'http://data-pipeline:9000/pipeline/etl-excel';

            try {
                $response = Http::timeout(180)->post($pipelineUrl);

                if ($response->successful()) {
                    $body = $response->json();
                    Notification::make()
                        ->success()
                        ->title('ChromaDB Sync Dimulai')
                        ->body($body['message'] ?? 'ETL pipeline berjalan. Cek log untuk detail.')
                        ->send();
                    return;
                }
            } catch (\Throwable $e) {
                // Pipeline endpoint not available, fall through
            }

            // Fallback: notify user to run manually
            Notification::make()
                ->warning()
                ->title('Sync Manual Diperlukan')
                ->body('Jalankan di server: docker compose exec data-pipeline python /app/etl_pipeline.py')
                ->persistent()
                ->send();

        } catch (\Throwable $e) {
            Notification::make()
                ->danger()
                ->title('ChromaDB Sync Gagal')
                ->body($e->getMessage())
                ->send();
        }
    }
}
