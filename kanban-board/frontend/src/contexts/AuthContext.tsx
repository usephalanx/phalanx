/**
 * AuthContext provider that stores tokens in localStorage and exposes
 * login, logout, register functions, isAuthenticated flag, and the current user state.
 *
 * On mount, the provider restores user from localStorage and verifies the
 * access token is still valid by calling /auth/me. If the token has expired,
 * it attempts a silent refresh using the stored refresh token. If both fail,
 * the user is logged out.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import apiClient from "../api/client";
import type {
  LoginRequest,
  RegisterRequest,
  TokenPair,
  RefreshResponse,
  User,
} from "../types/auth";

export interface AuthContextValue {
  /** The currently authenticated user, or null if not logged in. */
  user: User | null;
  /** Whether the user is authenticated (has a valid user object). */
  isAuthenticated: boolean;
  /** Whether the auth state is still being loaded from storage. */
  loading: boolean;
  /** Log in with email and password. Stores tokens and user in state. */
  login: (credentials: LoginRequest) => Promise<void>;
  /** Register a new account. Stores tokens and user in state. */
  register: (data: RegisterRequest) => Promise<void>;
  /** Log out. Clears tokens and user state. */
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

/**
 * Persist authentication tokens to localStorage.
 */
function persistTokens(tokens: TokenPair): void {
  localStorage.setItem("access_token", tokens.access_token);
  localStorage.setItem("refresh_token", tokens.refresh_token);
}

/**
 * Persist the user object to localStorage.
 */
function persistUser(user: User): void {
  localStorage.setItem("user", JSON.stringify(user));
}

/**
 * Clear all persisted authentication data from localStorage.
 */
function clearAuth(): void {
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
  localStorage.removeItem("user");
}

interface AuthProviderProps {
  children: ReactNode;
}

/**
 * Provider component that wraps the app to make auth state available.
 */
export function AuthProvider({ children }: AuthProviderProps): JSX.Element {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // On mount, restore user from localStorage and verify the token
  useEffect(() => {
    let cancelled = false;

    async function verifyAuth(): Promise<void> {
      const storedUser = localStorage.getItem("user");
      const accessToken = localStorage.getItem("access_token");

      if (!storedUser || !accessToken) {
        if (!cancelled) {
          setLoading(false);
        }
        return;
      }

      // Optimistically set the user from localStorage
      try {
        const parsed = JSON.parse(storedUser) as User;
        if (!cancelled) {
          setUser(parsed);
        }
      } catch {
        clearAuth();
        if (!cancelled) {
          setLoading(false);
        }
        return;
      }

      // Verify the access token is still valid by calling /auth/me
      try {
        const response = await apiClient.get<User>("/auth/me");
        if (!cancelled) {
          const freshUser = response.data;
          persistUser(freshUser);
          setUser(freshUser);
        }
      } catch {
        // Token may be expired — attempt a silent refresh
        const refreshToken = localStorage.getItem("refresh_token");
        if (refreshToken) {
          try {
            const refreshResponse = await apiClient.post<RefreshResponse>(
              "/auth/refresh",
              { refresh_token: refreshToken },
            );
            if (!cancelled) {
              localStorage.setItem("access_token", refreshResponse.data.access_token);

              // Re-fetch the user profile with the new token
              const meResponse = await apiClient.get<User>("/auth/me");
              const freshUser = meResponse.data;
              persistUser(freshUser);
              setUser(freshUser);
            }
          } catch {
            // Refresh also failed — clear auth
            clearAuth();
            if (!cancelled) {
              setUser(null);
            }
          }
        } else {
          clearAuth();
          if (!cancelled) {
            setUser(null);
          }
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void verifyAuth();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (credentials: LoginRequest): Promise<void> => {
    // Get token pair from login endpoint
    const tokenResponse = await apiClient.post<TokenPair>(
      "/auth/login",
      credentials,
    );
    persistTokens(tokenResponse.data);

    // Fetch full user profile
    const meResponse = await apiClient.get<User>("/auth/me");
    const userData = meResponse.data;
    persistUser(userData);
    setUser(userData);
  }, []);

  const register = useCallback(
    async (data: RegisterRequest): Promise<void> => {
      // Get token pair from register endpoint
      const tokenResponse = await apiClient.post<TokenPair>(
        "/auth/register",
        data,
      );
      persistTokens(tokenResponse.data);

      // Fetch full user profile
      const meResponse = await apiClient.get<User>("/auth/me");
      const userData = meResponse.data;
      persistUser(userData);
      setUser(userData);
    },
    [],
  );

  const logout = useCallback((): void => {
    clearAuth();
    setUser(null);
  }, []);

  const isAuthenticated = user !== null;

  const value = useMemo<AuthContextValue>(
    () => ({ user, isAuthenticated, loading, login, register, logout }),
    [user, isAuthenticated, loading, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/**
 * Hook to consume the AuthContext. Must be used within an AuthProvider.
 *
 * @returns The current auth context value.
 * @throws Error if used outside of an AuthProvider.
 */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
