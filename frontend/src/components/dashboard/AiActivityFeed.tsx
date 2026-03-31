import { Bot, MessageCircle, User, Zap } from "lucide-react";
import type { AiInteraction } from "@/types";
import { cn, formatRelativeTime } from "@/lib/utils";
import { ChatSkeleton } from "@/components/shared/LoadingSkeleton";
import { EmptyState } from "@/components/shared/EmptyState";

interface AiActivityFeedProps {
  interactions: AiInteraction[];
  isLoading: boolean;
}

function ChannelBadge({ channel }: { channel: string }) {
  const labels: Record<string, string> = {
    whatsapp: "WhatsApp",
    telegram: "Telegram",
    web: "Web",
    mobile: "Mobile",
    internal: "Internal",
  };

  return (
    <span className="rounded-full bg-green-50 px-2 py-0.5 text-[10px] font-medium text-green-700">
      {labels[channel] ?? channel}
    </span>
  );
}

export function AiActivityFeed({ interactions, isLoading }: AiActivityFeedProps) {
  if (isLoading) {
    return (
      <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
        <h2 className="mb-4 text-base font-bold text-gray-900">
          Riwayat Konsultasi AI
        </h2>
        <ChatSkeleton />
      </div>
    );
  }

  // Group interactions by session and take the most recent 10
  const recent = interactions.slice(0, 10);

  return (
    <div className="rounded-2xl border border-gray-100 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-gray-100 px-6 py-4">
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand-100">
            <Bot className="h-4 w-4 text-brand-600" />
          </div>
          <h2 className="text-base font-bold text-gray-900">
            Riwayat Konsultasi AI
          </h2>
        </div>
        {interactions.length > 0 && (
          <span className="rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-medium text-gray-600">
            {interactions.length} pesan
          </span>
        )}
      </div>

      {recent.length === 0 ? (
        <div className="p-6">
          <EmptyState
            icon={MessageCircle}
            title="Belum ada riwayat konsultasi"
            description="Mulai konsultasi perizinan Anda dengan asisten BIMA-AI melalui WhatsApp"
          />
        </div>
      ) : (
        <div className="divide-y divide-gray-50">
          {recent.map((interaction) => {
            const isUser = interaction.message_type === "user_message";
            const isSystem = interaction.message_type === "system_event";

            if (isSystem) {
              return (
                <div key={interaction.id} className="px-6 py-3 text-center">
                  <span className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-3 py-1 text-xs text-gray-500">
                    <Zap className="h-3 w-3" />
                    {interaction.content}
                  </span>
                </div>
              );
            }

            return (
              <div
                key={interaction.id}
                className={cn(
                  "flex gap-3 px-6 py-4",
                  isUser ? "flex-row-reverse" : "flex-row",
                )}
              >
                {/* Avatar */}
                <div
                  className={cn(
                    "flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full text-white",
                    isUser ? "bg-brand-600" : "bg-gray-700",
                  )}
                >
                  {isUser ? (
                    <User className="h-4 w-4" />
                  ) : (
                    <Bot className="h-4 w-4" />
                  )}
                </div>

                {/* Bubble */}
                <div
                  className={cn(
                    "max-w-[75%] space-y-1",
                    isUser ? "items-end" : "items-start",
                  )}
                >
                  <div className="flex items-center gap-2">
                    <ChannelBadge channel={interaction.channel} />
                    <span className="text-[10px] text-gray-400">
                      {formatRelativeTime(interaction.created_at)}
                    </span>
                  </div>
                  <div
                    className={cn(
                      "rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
                      isUser
                        ? "rounded-tr-md bg-brand-600 text-white"
                        : "rounded-tl-md bg-gray-100 text-gray-800",
                    )}
                  >
                    {interaction.content}
                  </div>
                  {interaction.intent && !isUser && (
                    <p className="text-[10px] text-gray-400">
                      Intent: {interaction.intent}
                    </p>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
