import useSWR from "swr";
import type { AiInteraction } from "@/types";
import { fetchAiLogs } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";

export function useAiLogs() {
  const { isAuthenticated } = useAuth();

  const { data, error, isLoading } = useSWR<AiInteraction[]>(
    isAuthenticated ? "ai-logs" : null,
    fetchAiLogs,
    { revalidateOnFocus: false, refreshInterval: 30000 },
  );

  return {
    interactions: data ?? [],
    isLoading,
    error,
  };
}
