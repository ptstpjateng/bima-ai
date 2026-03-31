<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    /**
     * Run the migrations.
     *
     * Logs every AI message exchange for auditing, context recall, and analytics.
     * Designed to support multi-channel (WhatsApp, Telegram, Web, Mobile) flows.
     */
    public function up(): void
    {
        Schema::create('ai_interactions', function (Blueprint $table) {
            $table->id();

            // Ownership (nullable to support pre-registration anonymous sessions)
            $table->foreignId('user_id')
                ->nullable()
                ->constrained()
                ->nullOnDelete()
                ->comment('Authenticated MSME user; null for anonymous pre-auth sessions');
            $table->foreignId('permit_application_id')
                ->nullable()
                ->constrained()
                ->nullOnDelete()
                ->comment('Linked permit application if this interaction is application-specific');

            // Session Tracking
            $table->string('session_id', 64)->index()->comment('Groups messages in a single conversation turn/session');
            $table->unsignedBigInteger('turn_index')->default(0)->comment('Message order within the session');

            // Channel & Actor
            $table->enum('channel', ['whatsapp', 'telegram', 'web', 'mobile', 'internal'])
                ->default('web')
                ->comment('Source channel through which the interaction originated');
            $table->enum('message_type', ['user_message', 'ai_response', 'system_event'])
                ->comment('Distinguishes who sent the message');

            // Message Content
            $table->text('content')->comment('Raw message text or system event description');
            $table->text('content_rendered')->nullable()->comment('HTML/Markdown rendered version for display');

            // AI Processing Metadata
            $table->string('intent', 100)->nullable()->comment('Detected user intent, e.g. "kbli_lookup", "form_help", "status_check"');
            $table->json('entities')->nullable()->comment('Extracted NLP entities: {kbli_code, business_type, location, etc.}');
            $table->json('context_snapshot')->nullable()->comment('Conversation state snapshot at the time of this message');
            $table->string('model_used', 80)->nullable()->comment('AI model identifier, e.g. "gpt-4o", "claude-3-5-sonnet"');
            $table->unsignedInteger('prompt_tokens')->nullable();
            $table->unsignedInteger('completion_tokens')->nullable();
            $table->unsignedInteger('response_time_ms')->nullable()->comment('End-to-end AI response latency in milliseconds');
            $table->decimal('confidence_score', 4, 3)->nullable()->comment('AI confidence 0.000-1.000 for intent classification');

            // External Message Reference (for idempotency with WhatsApp/Telegram webhooks)
            $table->string('external_message_id', 100)->nullable()->index()->comment('WhatsApp/Telegram message ID to prevent duplicate processing');

            // Audit & Safety
            $table->boolean('is_flagged')->default(false)->comment('Manually or automatically flagged for human review');
            $table->string('flag_reason', 200)->nullable();
            $table->boolean('is_sensitive')->default(false)->comment('Contains PII or sensitive business data');

            $table->timestamps();

            // Indexes for audit queries and session reconstruction
            $table->index(['session_id', 'turn_index']);
            $table->index(['user_id', 'created_at']);
            $table->index(['channel', 'created_at']);
            $table->index(['is_flagged', 'created_at']);
        });
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('ai_interactions');
    }
};
