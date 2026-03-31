import useSWR from "swr";
import type { PermitApplication } from "@/types";
import { fetchPermits } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";

export function usePermits() {
  const { user, isAuthenticated } = useAuth();

  const { data, error, isLoading, mutate } = useSWR<PermitApplication[]>(
    isAuthenticated && user ? ["permits", user.id] : null,
    () => fetchPermits(user!.id),
    { revalidateOnFocus: false },
  );

  return {
    permits: data ?? [],
    isLoading,
    error,
    refresh: mutate,
  };
}
