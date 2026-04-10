"use client";

import {
  Bot,
  ChevronDown,
  ExternalLink,
  Loader2,
  MessageCircle,
  Send,
  User,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { webChat } from "@/lib/api";
import { cn, formatRelativeTime } from "@/lib/utils";

interface Message {
  id: string;
  role: "user" | "ai";
  content: string;
  timestamp: string;
}

// userId prop removed — user identity is now server-verified via Bearer token

const PORTAL_URL = "https://project-5z22k.vercel.app";

// Strip markdown link syntax for display: [text](url) → text (url shown as button)
function ChatBubble({ content, role }: { content: string; role: "user" | "ai" }) {
  if (role === "user") {
    return <p className="text-sm leading-relaxed">{content}</p>;
  }

  // For AI: render **bold** and portal links as simple formatted text
  const parts = content.split(/(\[Buka Portal BIMA-AI →\]\([^)]+\))/g);

  return (
    <div className="space-y-2 text-sm leading-relaxed">
      {parts.map((part, i) => {
        if (part.startsWith("[Buka Portal BIMA-AI →]")) {
          return (
            <a
              key={i}
              href={PORTAL_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-lg bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-700 transition-colors"
            >
              Buka Portal BIMA-AI
              <ExternalLink className="h-3 w-3" />
            </a>
          );
        }
        // Render **bold** text
        const segments = part.split(/\*\*([^*]+)\*\*/g);
        return (
          <span key={i}>
            {segments.map((seg, j) =>
              j % 2 === 1 ? (
                <strong key={j} className="font-semibold text-gray-900">
                  {seg}
                </strong>
              ) : (
                <span key={j}>{seg}</span>
              ),
            )}
          </span>
        );
      })}
    </div>
  );
}

export function ChatWidget() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  // Focus input when opened
  useEffect(() => {
    if (open) {
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  async function handleSend() {
    const text = input.trim();
    if (!text || loading) return;

    const userMsg: Message = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
      timestamp: new Date().toISOString(),
    };

    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    try {
      const response = await webChat(text);
      const aiMsg: Message = {
        id: crypto.randomUUID(),
        role: "ai",
        content: response,
        timestamp: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, aiMsg]);
    } catch (err) {
      const errMsg: Message = {
        id: crypto.randomUUID(),
        role: "ai",
        content:
          err instanceof Error
            ? err.message
            : "Terjadi kesalahan. Silakan coba lagi.",
        timestamp: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, errMsg]);
    } finally {
      setLoading(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  return (
    <div className="rounded-2xl border border-gray-100 bg-white shadow-sm overflow-hidden">
      {/* Header — always visible, click to toggle */}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-6 py-4 hover:bg-gray-50 transition-colors"
      >
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-brand-600 to-brand-800 shadow-sm">
            <Bot className="h-4 w-4 text-white" />
          </div>
          <div className="text-left">
            <p className="text-sm font-bold text-gray-900">Tanya BIMA-AI</p>
            <p className="text-xs text-gray-500">
              {messages.length === 0
                ? "Konsultasi perizinan OSS RBA"
                : `${messages.length} pesan`}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {/* Online indicator */}
          <span className="flex items-center gap-1 rounded-full bg-green-50 px-2 py-0.5 text-[10px] font-medium text-green-700">
            <span className="h-1.5 w-1.5 rounded-full bg-green-500" />
            Online
          </span>
          <ChevronDown
            className={cn(
              "h-4 w-4 text-gray-400 transition-transform duration-200",
              open && "rotate-180",
            )}
          />
        </div>
      </button>

      {/* Collapsible body */}
      {open && (
        <>
          {/* Message list */}
          <div className="h-80 overflow-y-auto border-t border-gray-100 bg-gray-50 px-4 py-4">
            {messages.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-brand-100">
                  <MessageCircle className="h-6 w-6 text-brand-600" />
                </div>
                <div>
                  <p className="text-sm font-semibold text-gray-700">
                    Halo! Saya BIMA-AI
                  </p>
                  <p className="mt-1 text-xs text-gray-400">
                    Tanyakan apa saja tentang perizinan OSS RBA,<br />
                    KBLI, dokumen, atau kewajiban usaha Anda.
                  </p>
                </div>
                {/* Starter prompts */}
                <div className="mt-1 flex flex-wrap justify-center gap-2">
                  {[
                    "Apa itu NIB?",
                    "KBLI saya apa?",
                    "Dokumen apa yang diperlukan?",
                  ].map((prompt) => (
                    <button
                      key={prompt}
                      type="button"
                      onClick={() => {
                        setInput(prompt);
                        inputRef.current?.focus();
                      }}
                      className="rounded-full border border-brand-200 bg-white px-3 py-1 text-xs font-medium text-brand-700 hover:bg-brand-50 transition-colors"
                    >
                      {prompt}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                {messages.map((msg) => {
                  const isUser = msg.role === "user";
                  return (
                    <div
                      key={msg.id}
                      className={cn(
                        "flex gap-2.5",
                        isUser ? "flex-row-reverse" : "flex-row",
                      )}
                    >
                      {/* Avatar */}
                      <div
                        className={cn(
                          "flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full",
                          isUser
                            ? "bg-brand-600 text-white"
                            : "bg-gray-700 text-white",
                        )}
                      >
                        {isUser ? (
                          <User className="h-3.5 w-3.5" />
                        ) : (
                          <Bot className="h-3.5 w-3.5" />
                        )}
                      </div>

                      <div
                        className={cn(
                          "max-w-[80%]",
                          isUser ? "items-end" : "items-start",
                        )}
                      >
                        <div
                          className={cn(
                            "rounded-2xl px-4 py-2.5",
                            isUser
                              ? "rounded-tr-md bg-brand-600 text-white"
                              : "rounded-tl-md bg-white text-gray-800 shadow-sm",
                          )}
                        >
                          <ChatBubble content={msg.content} role={msg.role} />
                        </div>
                        <p
                          className={cn(
                            "mt-1 text-[10px] text-gray-400",
                            isUser ? "text-right" : "text-left",
                          )}
                        >
                          {formatRelativeTime(msg.timestamp)}
                        </p>
                      </div>
                    </div>
                  );
                })}

                {/* Typing indicator */}
                {loading && (
                  <div className="flex gap-2.5">
                    <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-gray-700">
                      <Bot className="h-3.5 w-3.5 text-white" />
                    </div>
                    <div className="rounded-2xl rounded-tl-md bg-white px-4 py-3 shadow-sm">
                      <div className="flex items-center gap-1.5">
                        <Loader2 className="h-3.5 w-3.5 animate-spin text-brand-500" />
                        <span className="text-xs text-gray-400">
                          BIMA-AI sedang mengetik...
                        </span>
                      </div>
                    </div>
                  </div>
                )}

                <div ref={bottomRef} />
              </div>
            )}
          </div>

          {/* Input area */}
          <div className="border-t border-gray-100 px-4 py-3">
            <div className="flex items-end gap-2">
              <textarea
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Ketik pertanyaan Anda... (Enter untuk kirim)"
                rows={1}
                disabled={loading}
                className="flex-1 resize-none rounded-xl border border-gray-200 bg-white px-4 py-2.5 text-sm text-gray-900 placeholder-gray-400 outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100 disabled:opacity-60"
                style={{ maxHeight: "120px" }}
                onInput={(e) => {
                  const el = e.currentTarget;
                  el.style.height = "auto";
                  el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
                }}
              />
              <button
                type="button"
                onClick={handleSend}
                disabled={!input.trim() || loading}
                className={cn(
                  "flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl transition-colors",
                  input.trim() && !loading
                    ? "bg-brand-600 text-white hover:bg-brand-700"
                    : "bg-gray-100 text-gray-400 cursor-not-allowed",
                )}
              >
                <Send className="h-4 w-4" />
              </button>
            </div>
            <p className="mt-1.5 text-[10px] text-gray-400">
              Respons AI biasanya membutuhkan 15–45 detik
            </p>
          </div>
        </>
      )}
    </div>
  );
}
